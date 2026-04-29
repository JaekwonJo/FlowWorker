from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .browser import BrowserManager
from .prompt_parser import PromptBlock, compress_numbers, load_prompt_blocks


LogFn = Callable[[str], None]
StatusFn = Callable[[str], None]
StatusDetailFn = Callable[[str], None]
QueueFn = Callable[[int, str, str, str], None]
StopFn = Callable[[], bool]
PauseFn = Callable[[], bool]
ActionFn = Callable[[str], None]


@dataclass
class RunPlan:
    items: list[PromptBlock]
    selection_summary: str


class FlowAutomationEngine:
    def __init__(self, base_dir: Path, cfg: dict):
        self.base_dir = Path(base_dir)
        self.cfg = cfg
        self._log_fn: LogFn = lambda _message: None
        self._status_fn: StatusFn = lambda _message: None
        self._status_detail_fn: StatusDetailFn = lambda _message: None
        self._action_fn: ActionFn = lambda _message: None

    def build_plan(self) -> RunPlan:
        prompt_slots = list(self.cfg.get("prompt_slots") or [])
        if not prompt_slots:
            return RunPlan(items=[], selection_summary="프롬프트 파일 없음")
        slot_index = max(0, min(int(self.cfg.get("prompt_slot_index", 0) or 0), len(prompt_slots) - 1))
        slot = prompt_slots[slot_index]
        path = self.base_dir / str(slot.get("file") or "")
        media_mode = str(self.cfg.get("media_mode") or "image").strip().lower()
        items = load_prompt_blocks(
            path,
            prefix=str(self.cfg.get("prompt_prefix") or "S"),
            pad_width=int(self.cfg.get("prompt_pad_width", 3) or 3),
            separator=str(self.cfg.get("prompt_separator") or "|||"),
            extra_prefixes=("V",) if media_mode == "video" else (),
        )
        selected = self._filter_items(items)
        return RunPlan(items=selected, selection_summary=self._selection_summary(selected, media_mode))

    def run(
        self,
        *,
        plan: RunPlan,
        log: LogFn,
        set_status: StatusFn,
        set_status_detail: StatusDetailFn,
        update_queue: QueueFn,
        should_stop: StopFn,
        is_paused: PauseFn,
        browser: BrowserManager,
        action_log: ActionFn | None = None,
    ) -> None:
        self._log_fn = log
        self._status_fn = set_status
        self._status_detail_fn = set_status_detail
        self._action_fn = action_log or (lambda _message: None)
        if not plan.items:
            set_status("선택된 작업 없음")
            set_status_detail("")
            return

        media_mode = str(self.cfg.get("media_mode") or "image").strip().lower()
        if media_mode != "image":
            set_status("비디오 모드는 다음 단계 예정")
            set_status_detail("")
            log("비디오 모드는 아직 연결 전입니다. 이번 단계는 이미지 모드 핵심 자동화만 먼저 붙였습니다.")
            return

        project = self._current_project()
        project_url = str(project.get("url") or self.cfg.get("flow_site_url") or "").strip()
        if not project_url:
            raise RuntimeError("Flow 프로젝트 URL이 비어 있습니다.")

        set_status("브라우저 준비 중")
        set_status_detail("FlowWorker 전용 Edge 연결 중")
        log(f"브라우저 연결 준비: {project.get('name', '프로젝트')}")
        page = browser.ensure_page(
            url=project_url,
            profile_dir=str(self.base_dir / str(self.cfg.get("browser_profile_dir") or "runtime/flow_worker_edge_profile")),
            attach_url=str(self.cfg.get("browser_attach_url") or "http://127.0.0.1:9333"),
            window_cfg=self.cfg,
        )
        try:
            page.bring_to_front()
        except Exception:
            pass

        input_selector_hint = str(self.cfg.get("input_selector") or "").strip()
        if not input_selector_hint:
            input_selector_hint = "#PINHOLE_TEXT_AREA_ELEMENT_ID, textarea, [role='textbox'], [contenteditable='true']"

        set_status("Flow 입력창 확인 중")
        set_status_detail("페이지와 입력창 상태 확인 중")
        if not self._wait_until_input_visible(page, input_selector_hint, timeout_sec=18):
            self._try_open_new_project_if_needed(page, input_selector_hint, log=log)
        input_locator = self._wait_for_prompt_input(page, input_selector_hint)
        if input_locator is None:
            raise RuntimeError("Flow 입력창을 찾지 못했습니다. 페이지가 완전히 열린 뒤 다시 시도해주세요.")

        self._apply_image_preset(page, input_locator, log=log)
        set_status("이미지 모드 준비 완료")
        set_status_detail("프롬프트 제출 준비됨")

        generate_wait = max(0.5, float(self.cfg.get("generate_wait_seconds", 10.0) or 10.0))
        next_wait = max(0.0, float(self.cfg.get("next_prompt_wait_seconds", 7.0) or 7.0))
        image_quality = self._download_quality("image")
        failed_count = 0

        for item in plan.items:
            if should_stop():
                set_status("중지됨")
                set_status_detail("")
                log("사용자 중지 요청으로 작업을 멈췄습니다.")
                return
            self._wait_if_paused(is_paused=is_paused, should_stop=should_stop, set_status=set_status, set_status_detail=set_status_detail)
            if should_stop():
                set_status("중지됨")
                set_status_detail("")
                return

            try:
                update_queue(item.number, "running", f"{item.tag} 프롬프트 입력 중", "")
                set_status(f"{item.tag} 입력 중")
                set_status_detail("사람처럼 한 글자씩 입력 중")
                input_locator = self._wait_for_prompt_input(page, input_selector_hint)
                if input_locator is None:
                    raise RuntimeError("프롬프트 입력창을 다시 찾지 못했습니다.")

                self._type_prompt(page, input_locator, item.rendered_prompt)
                before_text = self._read_input_text(input_locator)
                submit_locator, submit_hint = self._resolve_submit_locator_for_input(page, input_locator)
                if submit_locator is None:
                    raise RuntimeError("생성 버튼을 찾지 못했습니다.")

                indicator_before = self._is_generation_indicator_visible(page)
                submit_before_state = self._capture_submit_state(submit_locator)
                self._click_locator(page, submit_locator)
                started, reason = self._confirm_submission_started(
                    page,
                    input_locator,
                    before_text,
                    submit_locator=submit_locator,
                    submit_before_state=submit_before_state,
                    indicator_before=indicator_before,
                )
                if not started:
                    raise RuntimeError("생성 시작 확인에 실패했습니다.")

                log(f"생성 제출 완료: {item.tag} | 버튼={submit_hint or 'geometry'} | 확인={reason}")
                update_queue(item.number, "waiting", f"{item.tag} 생성 요청 완료 | {generate_wait:.1f}초 대기", "")
                set_status(f"{item.tag} 생성 대기 중")
                self._sleep_with_countdown(
                    total_seconds=generate_wait,
                    headline=f"{item.tag} 생성 대기 중",
                    detail_prefix="다운로드 전 대기",
                    set_status=set_status,
                    set_status_detail=set_status_detail,
                    should_stop=should_stop,
                    is_paused=is_paused,
                )
                if should_stop():
                    set_status("중지됨")
                    set_status_detail("")
                    return

                update_queue(item.number, "downloading", f"{item.tag} 다운로드 시도 중 | {image_quality}", "")
                set_status(f"{item.tag} 다운로드 중")
                set_status_detail(f"이미지 {image_quality} 저장 준비")
                saved_name = self._download_image_for_tag(page, item.tag, image_quality, log=log)
                update_queue(item.number, "success", f"다운로드 완료 | {saved_name}", saved_name)
                set_status(f"{item.tag} 다음 작업 대기")
                set_status_detail("다음 작업으로 넘어가기 전 정리 중")
                if next_wait > 0:
                    self._sleep_with_countdown(
                        total_seconds=next_wait,
                        headline=f"{item.tag} 다음 작업 대기",
                        detail_prefix="다음 프롬프트 준비",
                        set_status=set_status,
                        set_status_detail=set_status_detail,
                        should_stop=should_stop,
                        is_paused=is_paused,
                    )
            except Exception as exc:
                failed_count += 1
                update_queue(item.number, "failed", str(exc), "")
                log(f"실패: {item.tag} | {exc}")

        if failed_count > 0:
            set_status("이미지 모드 일부 실패")
            set_status_detail(f"실패 {failed_count}개 | 로그와 액션 트레이스 확인")
            log(f"이미지 모드: 일부 실패가 있어 액션 트레이스 확인이 필요합니다. 실패 {failed_count}개")
        else:
            set_status("이미지 모드 작업 완료")
            set_status_detail("생성 + 다운로드 원스톱 완료")
            log("이미지 모드: 프롬프트 제출과 이미지 다운로드까지 완료했습니다.")

    def _wait_if_paused(self, *, is_paused: PauseFn, should_stop: StopFn, set_status: StatusFn, set_status_detail: StatusDetailFn) -> None:
        announced = False
        while is_paused() and not should_stop():
            if not announced:
                set_status("일시정지")
                set_status_detail("사용자 재개를 기다리는 중")
                announced = True
            time.sleep(0.2)

    def _sleep_with_control(self, seconds: float, *, should_stop: StopFn, is_paused: PauseFn, set_status: StatusFn, set_status_detail: StatusDetailFn) -> None:
        end_at = time.time() + max(0.0, float(seconds))
        while time.time() < end_at:
            if should_stop():
                return
            if is_paused():
                self._wait_if_paused(is_paused=is_paused, should_stop=should_stop, set_status=set_status, set_status_detail=set_status_detail)
                end_at = time.time() + max(0.0, end_at - time.time())
            time.sleep(0.15)

    def _sleep_with_countdown(
        self,
        *,
        total_seconds: float,
        headline: str,
        detail_prefix: str,
        set_status: StatusFn,
        set_status_detail: StatusDetailFn,
        should_stop: StopFn,
        is_paused: PauseFn,
    ) -> None:
        end_at = time.time() + max(0.0, float(total_seconds))
        last_tick = None
        while time.time() < end_at:
            if should_stop():
                return
            if is_paused():
                self._wait_if_paused(is_paused=is_paused, should_stop=should_stop, set_status=set_status, set_status_detail=set_status_detail)
                end_at = time.time() + max(0.0, end_at - time.time())
            remaining = max(0.0, end_at - time.time())
            tick = int(remaining * 10)
            if tick != last_tick:
                set_status(headline)
                set_status_detail(f"{detail_prefix} | {remaining:.1f}초 남음")
                last_tick = tick
            time.sleep(0.1)
        set_status_detail("")

    def _current_project(self) -> dict:
        profiles = list(self.cfg.get("project_profiles") or [])
        idx = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1)) if profiles else 0
        return profiles[idx] if profiles else {"name": "기본 프로젝트", "url": self.cfg.get("flow_site_url", "")}

    def _emit_log(self, message: str) -> None:
        try:
            self._log_fn(str(message))
        except Exception:
            pass

    def _emit_status(self, message: str) -> None:
        try:
            self._status_fn(str(message))
        except Exception:
            pass

    def _emit_status_detail(self, message: str) -> None:
        try:
            self._status_detail_fn(str(message))
        except Exception:
            pass

    def _emit_action(self, message: str) -> None:
        try:
            self._action_fn(str(message))
        except Exception:
            pass

    def _normalize_candidate_list(self, raw) -> list[str]:
        if isinstance(raw, str):
            text = raw.strip()
            return [text] if text else []
        result: list[str] = []
        for item in raw or []:
            token = str(item or "").strip()
            if token:
                result.append(token)
        return result

    def _input_candidates(self) -> list[str]:
        cands = []
        cands.extend(self._normalize_candidate_list(self.cfg.get("input_selector", "")))
        cands.extend(
            [
                "#PINHOLE_TEXT_AREA_ELEMENT_ID",
                "textarea[placeholder*='무엇' i]",
                "textarea[placeholder*='prompt' i]",
                "textarea[aria-label*='prompt' i]",
                "textarea[aria-label*='message' i]",
                "textarea",
                "[role='textbox']",
                "[contenteditable='true']",
                "div[contenteditable='true']",
            ]
        )
        seen = set()
        uniq = []
        for item in cands:
            if item not in seen:
                uniq.append(item)
                seen.add(item)
        return uniq

    def _is_generic_input_selector(self, selector: str) -> bool:
        sel = str(selector or "").strip().lower()
        if not sel:
            return False
        return sel in {
            "textarea",
            "[contenteditable='true']",
            "div[contenteditable='true']",
            "[role='textbox']",
            "#pinhole_text_area_element_id, textarea, [role='textbox'], [contenteditable='true']",
        }

    def _locator_meta_text(self, locator) -> str:
        try:
            return locator.evaluate(
                """(el) => {
                    const a = (name) => (el.getAttribute(name) || "");
                    const parts = [
                        (el.tagName || ""),
                        (el.id || ""),
                        (el.className || ""),
                        a("name"),
                        a("placeholder"),
                        a("aria-label"),
                        a("title"),
                        (el.innerText || ""),
                    ];
                    return parts.join(" ").toLowerCase();
                }"""
            ) or ""
        except Exception:
            return ""

    def _is_asset_search_like_locator(self, locator) -> bool:
        meta = self._locator_meta_text(locator)
        if not meta:
            return False
        search_keys = ("asset", "search", "에셋", "검색")
        prompt_keys = ("무엇을 만들", "prompt", "프롬프트", "message", "메시지")
        has_search = any(key in meta for key in search_keys)
        has_prompt = any(key in meta for key in prompt_keys)
        return has_search and (not has_prompt)

    def _locator_prompt_input_score(self, page, locator, selector: str = "") -> float:
        try:
            info = locator.evaluate(
                """(el) => {
                    const a = (name) => (el.getAttribute(name) || "");
                    const text = (el.innerText || el.textContent || "");
                    const r = el.getBoundingClientRect();
                    return {
                        tag: (el.tagName || "").toLowerCase(),
                        role: a("role").toLowerCase(),
                        placeholder: a("placeholder").toLowerCase(),
                        aria: a("aria-label").toLowerCase(),
                        title: a("title").toLowerCase(),
                        name: a("name").toLowerCase(),
                        contenteditable: a("contenteditable").toLowerCase(),
                        text_len: text.length || 0,
                        rect: {x: r.x || 0, y: r.y || 0, width: r.width || 0, height: r.height || 0},
                    };
                }"""
            ) or {}
        except Exception:
            return float("-inf")

        rect = info.get("rect") or {}
        width = float(rect.get("width") or 0.0)
        height = float(rect.get("height") or 0.0)
        y = float(rect.get("y") or 0.0)
        if width < 40 or height < 18:
            return float("-inf")

        viewport_h = 900.0
        try:
            viewport_h = float(page.evaluate("window.innerHeight") or 900.0)
        except Exception:
            pass

        tag = str(info.get("tag") or "")
        role = str(info.get("role") or "")
        contenteditable = str(info.get("contenteditable") or "")
        text_len = int(info.get("text_len") or 0)
        meta = " ".join(
            [
                str(info.get("placeholder") or ""),
                str(info.get("aria") or ""),
                str(info.get("title") or ""),
                str(info.get("name") or ""),
                self._locator_meta_text(locator),
            ]
        ).lower()

        score = 0.0
        if any(key in meta for key in ("무엇을 만들", "prompt", "프롬프트", "message", "메시지")):
            score += 1700.0
        if any(key in meta for key in ("asset", "search", "에셋", "검색")):
            score -= 2400.0
        if tag == "textarea":
            score += 240.0
        if role == "textbox":
            score += 120.0
        if contenteditable in ("true", "plaintext-only"):
            score += 100.0
        if 180 <= width <= 1200:
            score += 120.0
        if 28 <= height <= 180:
            score += 220.0
        if width * height > 260000:
            score -= 1200.0
        center_y = y + (height / 2.0)
        y_ratio = center_y / max(1.0, viewport_h)
        if 0.45 <= y_ratio <= 0.97:
            score += 260.0
        elif y_ratio < 0.30:
            score -= 520.0
        if text_len > 160:
            score -= 700.0
        elif 0 <= text_len <= 12:
            score += 45.0
        if selector:
            if self._is_generic_input_selector(selector):
                score -= 120.0
            else:
                score += 90.0
        return score

    def _resolve_prompt_input_locator(self, page, input_selector: str, timeout_ms: int = 2500, near_locator=None):
        configured = self._normalize_candidate_list(input_selector)
        specific = [sel for sel in configured if not self._is_generic_input_selector(sel)]
        generic = [sel for sel in configured if self._is_generic_input_selector(sel)]

        candidates = []
        for sel in specific:
            if sel not in candidates:
                candidates.append(sel)
        for sel in self._input_candidates():
            if sel not in candidates:
                candidates.append(sel)
        for sel in generic:
            if sel not in candidates:
                candidates.append(sel)

        best = None
        best_selector = None
        best_score = float("-inf")
        near_box = None
        if near_locator is not None:
            try:
                near_box = near_locator.bounding_box()
            except Exception:
                near_box = None

        containers = [page]
        try:
            containers.extend(fr for fr in page.frames if fr != page.main_frame)
        except Exception:
            pass

        for container in containers:
            for sel in candidates:
                try:
                    loc = container.locator(sel)
                    total = loc.count()
                except Exception:
                    continue
                upper = min(total, 18)
                for idx in range(upper):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=timeout_ms):
                            continue
                    except Exception:
                        continue
                    if self._is_asset_search_like_locator(cand):
                        continue
                    score = self._locator_prompt_input_score(page, cand, sel)
                    if near_box:
                        try:
                            box = cand.bounding_box()
                        except Exception:
                            box = None
                        if box:
                            near_cx = float(near_box["x"]) + float(near_box["width"]) * 0.5
                            near_cy = float(near_box["y"]) + float(near_box["height"]) * 0.5
                            cx = float(box["x"]) + float(box["width"]) * 0.5
                            cy = float(box["y"]) + float(box["height"]) * 0.5
                            dist = ((cx - near_cx) ** 2 + (cy - near_cy) ** 2) ** 0.5
                            score -= dist * 0.9
                            if cy < (near_cy - 120.0):
                                score -= 1200.0
                    score -= idx * 6.0
                    if score > best_score:
                        best = cand
                        best_selector = sel
                        best_score = score
        return best, best_selector

    def _is_input_visible(self, page, input_selector: str) -> bool:
        loc, _ = self._resolve_prompt_input_locator(page, input_selector, timeout_ms=1200)
        return loc is not None

    def _wait_until_input_visible(self, page, input_selector: str, timeout_sec: int = 18) -> bool:
        end_ts = time.time() + max(1, timeout_sec)
        while time.time() < end_ts:
            if self._is_input_visible(page, input_selector):
                return True
            time.sleep(0.6)
        return False

    def _try_open_new_project_if_needed(self, page, input_selector: str, *, log: LogFn) -> bool:
        if self._is_input_visible(page, input_selector):
            return True
        candidates = [
            "button:has-text('새 프로젝트')",
            "button:has-text('새 프로젝트 만들기')",
            "[role='button']:has-text('새 프로젝트')",
            "a:has-text('새 프로젝트')",
            "button:has-text('Create')",
            "[role='button']:has-text('Create')",
            "button:has-text('New project')",
            "[role='button']:has-text('New project')",
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if loc.count() <= 0 or not loc.is_visible(timeout=600):
                    continue
                self._click_locator(page, loc)
                log(f"새 프로젝트 버튼 클릭: {sel}")
                time.sleep(1.0)
                return self._wait_until_input_visible(page, input_selector, timeout_sec=8)
            except Exception:
                continue
        return False

    def _wait_for_prompt_input(self, page, input_selector: str):
        locator = None
        for delay_sec in (0.0, 0.35, 0.8, 1.4):
            if delay_sec > 0:
                time.sleep(delay_sec)
            locator, _ = self._resolve_prompt_input_locator(page, input_selector, timeout_ms=1600)
            if locator is not None:
                return locator
        return None

    def _click_locator(self, page, locator) -> None:
        try:
            locator.scroll_into_view_if_needed(timeout=1200)
        except Exception:
            pass
        try:
            locator.click(timeout=3000)
            return
        except Exception:
            pass
        try:
            locator.evaluate("(el) => el.click()")
            return
        except Exception:
            pass
        box = locator.bounding_box()
        if not box:
            raise RuntimeError("클릭 대상 좌표를 찾지 못했습니다.")
        page.mouse.click(box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0)

    def _open_generation_panel(self, page, input_locator) -> bool:
        if self._find_button_with_labels(page, ("이미지", "image", "동영상", "video"), near_locator=input_locator)[0] is not None:
            return True
        toggle = self._resolve_generation_panel_toggle(page, input_locator)
        if toggle is None:
            return False
        self._click_locator(page, toggle)
        time.sleep(0.45)
        return self._find_button_with_labels(page, ("이미지", "image", "동영상", "video"), near_locator=input_locator)[0] is not None

    def _resolve_generation_panel_toggle(self, page, input_locator):
        try:
            ib = input_locator.bounding_box()
        except Exception:
            ib = None
        if not ib:
            return None
        input_left = ib["x"]
        input_right = ib["x"] + ib["width"]
        input_top = ib["y"]
        input_mid_y = ib["y"] + ib["height"] / 2.0
        target_x = max(input_left + ib["width"] * 0.68, input_right - 150.0)
        best = None
        best_score = float("inf")
        try:
            loc = page.locator("button, [role='button']")
            total = loc.count()
        except Exception:
            return None
        for i in range(min(total, 220)):
            cand = loc.nth(i)
            try:
                if not cand.is_visible(timeout=600):
                    continue
            except Exception:
                continue
            try:
                box = cand.bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            if box["width"] < 70 or box["height"] < 24 or box["width"] > 320 or box["height"] > 80:
                continue
            cx = box["x"] + box["width"] / 2.0
            cy = box["y"] + box["height"] / 2.0
            if cy < (input_top - 20) or cy > (input_top + ib["height"] + 70):
                continue
            if cx < (input_left + ib["width"] * 0.45):
                continue
            meta = self._locator_meta_text(cand)
            if any(x in meta for x in ("생성", "generate", "submit", "send", "보내", "설정", "도움", "help", "프로젝트", "검색", "search")):
                continue
            has_count_chip = any(x in meta for x in ("x1", "x2", "x3", "x4"))
            has_mode_hint = any(x in meta for x in ("nano banana", "이미지", "image", "동영상", "video", "veo"))
            if not (has_count_chip or has_mode_hint):
                continue
            score = abs(cx - target_x) + (abs(cy - input_mid_y) * 3.2)
            if has_count_chip:
                score -= 220.0
            if has_mode_hint:
                score -= 180.0
            if score < best_score:
                best = cand
                best_score = score
        return best

    def _find_button_with_labels(self, page, labels: tuple[str, ...], near_locator=None):
        labels_lower = tuple(str(label or "").strip().lower() for label in labels if str(label or "").strip())
        near_box = None
        if near_locator is not None:
            try:
                near_box = near_locator.bounding_box()
            except Exception:
                near_box = None
        best = None
        best_label = ""
        best_score = float("inf")
        containers = [page]
        try:
            containers.extend(fr for fr in page.frames if fr != page.main_frame)
        except Exception:
            pass
        for container in containers:
            try:
                loc = container.locator("button, [role='button'], [role='tab']")
                total = loc.count()
            except Exception:
                continue
            for idx in range(min(total, 220)):
                cand = loc.nth(idx)
                try:
                    if not cand.is_visible(timeout=500):
                        continue
                except Exception:
                    continue
                meta = self._locator_meta_text(cand)
                matched = next((label for label in labels_lower if label and label in meta), "")
                if not matched:
                    continue
                try:
                    box = cand.bounding_box()
                except Exception:
                    box = None
                if not box:
                    continue
                score = float(idx)
                if near_box:
                    near_cx = float(near_box["x"]) + float(near_box["width"]) * 0.5
                    near_cy = float(near_box["y"]) + float(near_box["height"]) * 0.5
                    cx = float(box["x"]) + float(box["width"]) * 0.5
                    cy = float(box["y"]) + float(box["height"]) * 0.5
                    score += ((cx - near_cx) ** 2 + (cy - near_cy) ** 2) ** 0.5 * 0.3
                    if cy < near_cy - 160:
                        score += 700.0
                if any(x in meta for x in ("생성", "generate", "submit", "send", "보내")) and matched not in ("image", "이미지", "video", "동영상", "x1", "x2", "x3", "x4"):
                    continue
                if score < best_score:
                    best = cand
                    best_label = matched
                    best_score = score
        return best, best_label

    def _apply_image_preset(self, page, input_locator, *, log: LogFn) -> None:
        if not self._open_generation_panel(page, input_locator):
            log("생성 옵션 패널을 자동으로 열지 못했습니다. 현재 보이는 설정 그대로 진행합니다.")
            return
        image_button, _ = self._find_button_with_labels(page, ("이미지", "image"), near_locator=input_locator)
        if image_button is not None:
            self._click_locator(page, image_button)
            time.sleep(0.3)
        variant = str(self.cfg.get("image_variant_count") or "x1").strip().lower()
        variant_button, _ = self._find_button_with_labels(page, (variant,), near_locator=input_locator)
        if variant_button is not None:
            self._click_locator(page, variant_button)
            time.sleep(0.25)
            log(f"이미지 생성 개수 설정: {variant}")
        else:
            log(f"이미지 생성 개수 버튼을 찾지 못했습니다: {variant}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.2)

    def _read_input_text(self, input_locator) -> str:
        if input_locator is None:
            return ""
        try:
            val = input_locator.evaluate(
                """(el) => {
                    if (!el) return "";
                    if ("value" in el && typeof el.value === "string") return el.value;
                    return (el.innerText || el.textContent || "");
                }"""
            )
            return (val or "").strip()
        except Exception:
            return ""

    def _normalize_reference_asset_tag(self, value) -> str:
        raw = str(value or "").strip().upper()
        if not raw:
            return ""
        prefix = str(self.cfg.get("prompt_prefix") or "S").strip().upper() or "S"
        pad_width = max(1, int(self.cfg.get("prompt_pad_width", 3) or 3))
        aliases = {prefix, "S", "V"}
        pattern = "|".join(re.escape(token) for token in sorted(aliases) if token)
        match = re.match(rf"^\s*(?:(?P<prefix>{pattern})\s*)?0*(?P<number>[1-9][0-9]*)\s*$", raw, re.IGNORECASE)
        if not match:
            return raw
        number = int(match.group("number"))
        return f"{prefix}{str(number).zfill(pad_width)}"

    def _split_prompt_inline_reference_parts(self, prompt_text: str) -> list[dict]:
        text = str(prompt_text or "")
        pattern = re.compile(r"@(S?\d{3,4})(?!\d)", re.IGNORECASE)
        parts: list[dict] = []
        cursor = 0
        for match in pattern.finditer(text):
            if match.start() > cursor:
                parts.append({"type": "text", "value": text[cursor:match.start()]})
            parts.append(
                {
                    "type": "reference",
                    "value": self._normalize_reference_asset_tag(match.group(1)),
                    "raw": match.group(0),
                }
            )
            cursor = match.end()
        if cursor < len(text):
            parts.append({"type": "text", "value": text[cursor:]})
        return parts

    def _type_prompt_inline_text_chunk(self, page, text: str) -> None:
        self._emit_action(f"inline 텍스트 직선 입력 시작 | len={len(str(text or ''))}")
        for ch in str(text or ""):
            if ch == "\n":
                page.keyboard.press("Shift+Enter")
                time.sleep(random.uniform(0.04, 0.10))
                continue
            delay = max(6, int(self._typing_delay_ms() * random.uniform(0.65, 1.15)))
            try:
                page.keyboard.type(ch, delay=delay)
            except Exception:
                page.keyboard.insert_text(ch)
            if ch in (" ", "\n"):
                time.sleep(random.uniform(0.01, 0.05))
            elif ch in (".", ",", "!", "?", ":", ";", ")", "(", "]", "["):
                time.sleep(random.uniform(0.02, 0.08))
            else:
                time.sleep(random.uniform(0.008, 0.045))
        self._emit_action("inline 텍스트 직선 입력 완료")

    def _prompt_reference_search_input_candidates(self) -> list[str]:
        cands = []
        cands.extend(self._normalize_candidate_list(self.cfg.get("prompt_reference_search_input_selector", "")))
        cands.extend(
            [
                "input",
                "input[type='search']",
                "textarea",
                "[role='searchbox']",
                "[role='textbox']",
                "[contenteditable='true']",
                "[contenteditable='plaintext-only']",
            ]
        )
        seen = set()
        uniq = []
        for item in cands:
            if item not in seen:
                uniq.append(item)
                seen.add(item)
        return uniq

    def _is_prompt_reference_overlay_input_box(self, box) -> bool:
        if not box:
            return False
        try:
            width = float(box.get("width") or 0.0)
            height = float(box.get("height") or 0.0)
            x = float(box.get("x") or 0.0)
            y = float(box.get("y") or 0.0)
        except Exception:
            return False
        if width < 180.0 or width > 980.0:
            return False
        if height < 18.0 or height > 40.0:
            return False
        if y < 8.0 or y > 560.0:
            return False
        if x < 40.0 or x > 980.0:
            return False
        return True

    def _resolve_prompt_reference_search_overlay_input(self, page, timeout_sec: float = 2.0):
        end_ts = time.time() + max(0.8, float(timeout_sec))
        best_dump: list[tuple[float, str, str, dict]] = []
        while time.time() < end_ts:
            best = None
            best_sel = None
            best_score = float("-inf")
            dump_rows: list[tuple[float, str, str, dict]] = []
            for sel in self._prompt_reference_search_input_candidates():
                try:
                    loc = page.locator(sel)
                    total = min(loc.count(), 40)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=200):
                            continue
                        box = cand.bounding_box()
                    except Exception:
                        continue
                    if not box:
                        continue
                    meta = self._locator_meta_text(cand)
                    overlay_shape = self._is_prompt_reference_overlay_input_box(box)
                    meta_has_search = any(key in meta for key in ("검색", "search", "asset", "에셋", "recent", "최근"))
                    if (not overlay_shape) and (not meta_has_search):
                        continue
                    score = 0.0
                    if overlay_shape:
                        score += 260.0
                    if meta_has_search:
                        score += 520.0
                    if any(key in meta for key in ("무엇을 만들", "prompt", "프롬프트", "message", "메시지")):
                        score -= 1800.0
                    if any(key in meta for key in ("nano banana", "veo", "video", "동영상", "이미지", "x1", "x2", "x3", "x4")):
                        score -= 1200.0
                    score -= abs((float(box["x"]) + float(box["width"]) * 0.5) - 420.0) * 0.22
                    if float(box["y"]) <= 180.0:
                        score += 120.0
                    elif float(box["y"]) <= 420.0:
                        score += 240.0
                    dump_rows.append((score, sel, meta[:120], {"x": float(box["x"]), "y": float(box["y"]), "width": float(box["width"]), "height": float(box["height"])}))
                    if score > best_score:
                        best = cand
                        best_sel = sel
                        best_score = score
            if dump_rows:
                best_dump = sorted(dump_rows, key=lambda row: row[0], reverse=True)[:8]
            if best is not None and best_score > 120.0:
                self._emit_action("레퍼런스 검색창 후보 상위")
                for idx, row in enumerate(best_dump, start=1):
                    box = row[3]
                    self._emit_action(
                        f"  {idx:02d}. score={row[0]:.1f} sel={row[1]} meta='{row[2]}' "
                        f"box=({box['x']:.1f},{box['y']:.1f},{box['width']:.1f},{box['height']:.1f})"
                    )
                return best, best_sel or ""
            time.sleep(0.12)
        if best_dump:
            self._emit_action("레퍼런스 검색창 후보 덤프(실패)")
            for idx, row in enumerate(best_dump, start=1):
                box = row[3]
                self._emit_action(
                    f"  {idx:02d}. score={row[0]:.1f} sel={row[1]} meta='{row[2]}' "
                    f"box=({box['x']:.1f},{box['y']:.1f},{box['width']:.1f},{box['height']:.1f})"
                )
        return None, None

    def _direct_fill_prompt_reference_search_via_dom(self, page, asset_tag: str):
        if not asset_tag:
            return False, "empty-tag"
        try:
            result = page.evaluate(
                """(payload) => {
                    const tag = String(payload.tag || "").trim();
                    const searchKeys = ["asset", "search", "에셋", "검색", "recent", "최근"];
                    const negativeKeys = ["무엇을 만들", "prompt", "프롬프트", "message", "메시지", "project", "title", "이름"];
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 10 || r.height < 10) return false;
                        const st = window.getComputedStyle(el);
                        return st && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => {
                        const a = (k) => (el.getAttribute(k) || "");
                        return [
                            el.tagName || "",
                            el.id || "",
                            el.className || "",
                            a("name"),
                            a("placeholder"),
                            a("aria-label"),
                            a("title"),
                            (el.innerText || ""),
                        ].join(" ").toLowerCase();
                    };
                    let best = null;
                    let bestScore = -1e9;
                    const nodes = document.querySelectorAll("input, textarea, [role='searchbox'], [role='textbox'], [contenteditable='true']");
                    for (const el of nodes) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 180 || r.width > 980) continue;
                        if (r.height < 18 || r.height > 40) continue;
                        if (r.top < 8 || r.top > 560) continue;
                        const meta = metaText(el);
                        let score = 0;
                        if (searchKeys.some((k) => meta.includes(k))) score += 600;
                        if (negativeKeys.some((k) => meta.includes(k))) score -= 1800;
                        if ((el.tagName || "").toLowerCase() === "input") score += 120;
                        if ((el.getAttribute("type") || "").toLowerCase() === "search") score += 220;
                        score -= Math.abs((r.left + r.width / 2) - 420) * 0.22;
                        if (score > bestScore) {
                            best = el;
                            bestScore = score;
                        }
                    }
                    if (!best || bestScore < 120) return {ok:false, reason:"overlay-search-input-not-found"};
                    best.focus();
                    try {
                        if ("value" in best) {
                            best.value = "";
                            best.dispatchEvent(new Event("input", {bubbles:true}));
                            best.value = tag;
                            best.dispatchEvent(new Event("input", {bubbles:true}));
                            best.dispatchEvent(new Event("change", {bubbles:true}));
                        } else {
                            best.textContent = "";
                            best.dispatchEvent(new InputEvent("input", {bubbles:true, data:""}));
                            best.textContent = tag;
                            best.dispatchEvent(new InputEvent("input", {bubbles:true, data:tag}));
                        }
                        return {ok:true, reason:"dom-filled"};
                    } catch (e) {
                        return {ok:false, reason:String(e)};
                    }
                }""",
                {"tag": asset_tag},
            )
        except Exception as exc:
            return False, str(exc)
        return bool(result and result.get("ok")), str((result or {}).get("reason") or "")

    def _open_prompt_reference_search_via_keyboard(self, page, input_locator, timeout_sec: float = 2.4):
        if input_locator is None:
            raise RuntimeError("프롬프트 입력창이 없어 @ 레퍼런스 호출을 할 수 없습니다.")
        try:
            input_locator.focus(timeout=1200)
        except Exception:
            pass
        deadline = time.time() + max(1.0, float(timeout_sec))
        last_error = "search-input-not-found"
        methods = ("page_type_at", "locator_type_at", "page_shift2", "locator_shift2", "js_dispatch")
        while time.time() < deadline:
            for method in methods:
                before_text = self._read_input_text(input_locator)
                try:
                    if method == "page_type_at":
                        self._emit_action("레퍼런스 @ 트리거 입력: page type('@')")
                        page.keyboard.type("@")
                    elif method == "locator_type_at":
                        self._emit_action("레퍼런스 @ 트리거 입력: locator type('@')")
                        input_locator.type("@", delay=random.randint(24, 70), timeout=1200)
                    elif method == "page_shift2":
                        self._emit_action("레퍼런스 @ 트리거 입력: page Shift+2")
                        page.keyboard.down("Shift")
                        page.keyboard.press("2")
                        page.keyboard.up("Shift")
                    elif method == "locator_shift2":
                        self._emit_action("레퍼런스 @ 트리거 입력: locator Shift+2")
                        input_locator.press("Shift+2", timeout=1200)
                    else:
                        self._emit_action("레퍼런스 @ 트리거 입력: js dispatch")
                        page.evaluate(
                            """() => {
                                const el = document.activeElement;
                                if (!el) return false;
                                const fireKey = (type) => el.dispatchEvent(new KeyboardEvent(type, {
                                    key: "@",
                                    code: "Digit2",
                                    shiftKey: true,
                                    bubbles: true,
                                    cancelable: true,
                                }));
                                fireKey("keydown");
                                try {
                                    if ("value" in el) {
                                        const start = el.selectionStart ?? String(el.value || "").length;
                                        const end = el.selectionEnd ?? start;
                                        const next = String(el.value || "").slice(0, start) + "@" + String(el.value || "").slice(end);
                                        el.value = next;
                                        if (el.setSelectionRange) el.setSelectionRange(start + 1, start + 1);
                                    } else if (el.isContentEditable) {
                                        document.execCommand("insertText", false, "@");
                                    }
                                } catch (e) {}
                                el.dispatchEvent(new InputEvent("input", {bubbles:true, data:"@", inputType:"insertText"}));
                                fireKey("keyup");
                                return true;
                            }"""
                        )
                except Exception as exc:
                    last_error = str(exc)
                time.sleep(random.uniform(0.25, 0.55))
                after_text = self._read_input_text(input_locator)
                typed_at = after_text.endswith("@") or (after_text.count("@") > before_text.count("@"))
                search_input, search_sel = self._resolve_prompt_reference_search_overlay_input(page, timeout_sec=0.9)
                if search_input is not None and typed_at:
                    self._emit_log(f"🔡 레퍼런스 @ 호출 성공: {method} -> {search_sel or '자동 탐색'}")
                    self._emit_action(f"레퍼런스 @ 호출 성공: {method} -> {search_sel or '자동 탐색'}")
                    return search_input, search_sel or ""
                try:
                    input_locator.focus(timeout=800)
                    current_text = self._read_input_text(input_locator)
                    extra_count = max(0, len(current_text) - len(before_text))
                    if extra_count > 0 and current_text.startswith(before_text):
                        for _ in range(extra_count):
                            page.keyboard.press("Backspace")
                    elif current_text.endswith("@"):
                        page.keyboard.press("Backspace")
                except Exception:
                    pass
                self._emit_action(f"레퍼런스 @ 호출 재시도: {method}")
                time.sleep(0.10)
        raise RuntimeError(f"@ 레퍼런스 검색창 호출 실패 ({last_error})")

    def _fill_prompt_reference_search_input(self, page, search_input, asset_tag: str):
        expected = self._normalize_reference_asset_tag(asset_tag)
        used_selector = ""

        def _try_fill(loc, selector_hint: str = ""):
            nonlocal used_selector
            if loc is None:
                return None
            try:
                box = loc.bounding_box()
            except Exception:
                box = None
            if (box is not None) and (not self._is_prompt_reference_overlay_input_box(box)):
                return None
            try:
                loc.click(timeout=350)
            except Exception:
                try:
                    loc.focus(timeout=300)
                except Exception:
                    return None
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            except Exception:
                pass
            try:
                loc.fill(asset_tag, timeout=500)
                used_selector = selector_hint
            except Exception:
                try:
                    loc.type(asset_tag, delay=random.randint(25, 70), timeout=1000)
                    used_selector = selector_hint
                except Exception:
                    return None
            time.sleep(0.08)
            typed = self._normalize_reference_asset_tag(self._read_input_text(loc))
            if typed == expected:
                return loc
            return None

        filled = _try_fill(search_input)
        if filled is None:
            retry_input, retry_sel = self._resolve_prompt_reference_search_overlay_input(page, timeout_sec=1.0)
            filled = _try_fill(retry_input, retry_sel or "")
        if filled is None:
            ok_dom, reason_dom = self._direct_fill_prompt_reference_search_via_dom(page, asset_tag)
            if not ok_dom:
                self._emit_action(f"레퍼런스 검색창 직접입력 실패: {reason_dom}")
                raise RuntimeError(f"레퍼런스 검색창을 찾지 못했습니다. ({reason_dom})")
            used_selector = used_selector or "DOM 직접입력"
            self._emit_log(f"🔎 레퍼런스 검색 입력: {asset_tag} ({used_selector})")
            self._emit_action(f"레퍼런스 검색 입력: {asset_tag} ({used_selector})")
            return None, used_selector
        used_selector = used_selector or "자동 탐색"
        self._emit_log(f"🔎 레퍼런스 검색 입력: {asset_tag} ({used_selector})")
        self._emit_action(f"레퍼런스 검색 입력: {asset_tag} ({used_selector})")
        return filled, used_selector

    def _resolve_prompt_reference_sort_button(self, page, search_input=None, timeout_sec: float = 1.6):
        try:
            search_box = search_input.bounding_box() if search_input is not None else None
        except Exception:
            search_box = None
        labels = ("최근 사용", "가장 많이 사용", "최신순", "최신 순", "오래된 순")
        end_ts = time.time() + max(1.0, float(timeout_sec))
        while time.time() < end_ts:
            best = None
            best_score = float("-inf")
            best_sel = ""
            for sel in ("button", "[role='button']", "div[role='button']", "[role='combobox']", "[aria-haspopup='menu']", "[aria-expanded]"):
                try:
                    loc = page.locator(sel)
                    total = min(loc.count(), 120)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=250):
                            continue
                        box = cand.bounding_box()
                    except Exception:
                        continue
                    if not box:
                        continue
                    meta = self._locator_meta_text(cand)
                    if not any(label in meta for label in labels):
                        continue
                    score = 240.0
                    if search_box:
                        score -= abs(float(box["y"]) - float(search_box["y"])) * 1.2
                        score -= abs((float(box["x"]) + float(box["width"]) * 0.5) - (float(search_box["x"]) + float(search_box["width"]) + 70.0)) * 0.18
                    if "최근 사용" in meta:
                        score += 120.0
                    if "오래된 순" in meta or "최신순" in meta or "최신 순" in meta:
                        score += 80.0
                    if score > best_score:
                        best = cand
                        best_score = score
                        best_sel = sel
            if best is not None and best_score > -300.0:
                return best, best_sel
            time.sleep(0.10)
        return None, None

    def _resolve_prompt_reference_sort_option(self, page, order: str = "oldest", timeout_sec: float = 1.6, anchor_box=None):
        labels = ("최신순", "최신 순") if str(order or "").strip().lower() == "latest" else ("오래된 순",)
        end_ts = time.time() + max(1.0, float(timeout_sec))
        while time.time() < end_ts:
            matches = []
            for sel in ("button", "[role='button']", "div[role='button']", "[role='menuitem']", "li"):
                try:
                    loc = page.locator(sel)
                    total = min(loc.count(), 50)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=250):
                            continue
                        box = cand.bounding_box()
                    except Exception:
                        continue
                    if not box:
                        continue
                    meta = self._locator_meta_text(cand)
                    if not any(label in meta for label in labels):
                        continue
                    score = float(box["y"])
                    if anchor_box:
                        try:
                            score += abs(float(box["x"]) - float(anchor_box.get("x") or 0.0)) * 0.08
                        except Exception:
                            pass
                    matches.append((score, cand, sel))
            if matches:
                matches.sort(key=lambda row: row[0])
                return matches[0][1], matches[0][2]
            time.sleep(0.10)
        return None, None

    def _set_prompt_reference_sort_oldest(self, page, search_input=None):
        sort_button, sort_sel = self._resolve_prompt_reference_sort_button(page, search_input=search_input, timeout_sec=1.4)
        if sort_button is None:
            self._emit_log("⚠️ 레퍼런스 정렬 버튼을 찾지 못해 기본 정렬로 계속합니다.")
            self._emit_action("레퍼런스 정렬 버튼 미탐지")
            return search_input
        try:
            sort_meta = self._locator_meta_text(sort_button)
        except Exception:
            sort_meta = ""
        if "오래된 순" not in sort_meta:
            try:
                sort_box = sort_button.bounding_box()
            except Exception:
                sort_box = None
            self._click_locator(page, sort_button)
            self._emit_log(f"↕️ 레퍼런스 정렬 버튼 클릭: {sort_sel or '자동 탐색'}")
            self._emit_action(f"레퍼런스 정렬 버튼 클릭: {sort_sel or '자동 탐색'}")
            time.sleep(random.uniform(0.10, 0.22))
            order_button, _ = self._resolve_prompt_reference_sort_option(page, order="oldest", timeout_sec=1.4, anchor_box=sort_box)
            if order_button is not None:
                self._click_locator(page, order_button)
                self._emit_log("↕️ 레퍼런스 정렬 선택: 오래된 순")
                self._emit_action("레퍼런스 정렬 선택: 오래된 순")
                time.sleep(random.uniform(0.10, 0.20))
        refreshed_input, _ = self._resolve_prompt_reference_search_overlay_input(page, timeout_sec=1.0)
        return refreshed_input or search_input

    def _attach_prompt_reference_asset(self, page, input_locator, asset_tag: str):
        asset_tag = self._normalize_reference_asset_tag(asset_tag)
        if (not asset_tag) or input_locator is None:
            return input_locator
        self._emit_status(f"{asset_tag} 레퍼런스 첨부 중")
        self._emit_status_detail("Flow Classic Plus 방식으로 @ 태그 첨부 중")
        self._emit_log(f"🔖 레퍼런스 첨부 시작: {asset_tag}")
        self._emit_action(f"레퍼런스 첨부 시작: {asset_tag}")
        search_input, _ = self._open_prompt_reference_search_via_keyboard(page, input_locator, timeout_sec=2.4)
        search_input = self._set_prompt_reference_sort_oldest(page, search_input=search_input)
        search_input, search_sel = self._fill_prompt_reference_search_input(page, search_input, asset_tag)
        if search_sel:
            self.cfg["prompt_reference_search_input_selector"] = search_sel
        time.sleep(random.uniform(0.04, 0.10))
        page.keyboard.press("Enter")
        self._emit_log(f"✅ 레퍼런스 첨부 요청 완료: {asset_tag}")
        self._emit_action(f"레퍼런스 Enter 선택: {asset_tag}")
        time.sleep(random.uniform(0.08, 0.18))
        return input_locator

    def _type_prompt_with_inline_references(self, page, input_locator, prompt_text: str) -> None:
        parts = self._split_prompt_inline_reference_parts(prompt_text)
        if not any(part.get("type") == "reference" for part in parts):
            self._type_text_human_like(page=page, text=prompt_text)
            return
        self._emit_log(f"🔖 프롬프트 inline 레퍼런스 감지: {sum(1 for part in parts if part.get('type') == 'reference')}개")
        keep_focus_only = False
        total_parts = len(parts)
        for idx, part in enumerate(parts):
            part_type = str(part.get("type") or "")
            value = str(part.get("value") or "")
            if part_type == "text":
                if not value:
                    continue
                if keep_focus_only:
                    next_has_reference = any(later.get("type") == "reference" for later in parts[idx + 1 :])
                    if next_has_reference:
                        self._type_prompt_inline_text_chunk(page, value)
                    else:
                        protected_len = min(len(value), 18)
                        protected_chunk = value[:protected_len]
                        remaining_chunk = value[protected_len:]
                        if protected_chunk:
                            self._type_prompt_inline_text_chunk(page, protected_chunk)
                        if remaining_chunk:
                            self._type_text_human_like(page=page, text=remaining_chunk)
                else:
                    self._type_text_human_like(page=page, text=value)
                keep_focus_only = True
                continue
            if part_type == "reference" and value:
                self._emit_status_detail(f"inline 레퍼런스 첨부 중 | {value}")
                input_locator = self._attach_prompt_reference_asset(page, input_locator, value)
                keep_focus_only = True
                if idx == total_parts - 1:
                    time.sleep(0.06)

    def _type_prompt(self, page, input_locator, prompt_text: str) -> None:
        try:
            input_locator.scroll_into_view_if_needed(timeout=1200)
        except Exception:
            pass
        self._click_locator(page, input_locator)
        time.sleep(0.15)
        for combo in ("Control+A", "Meta+A"):
            try:
                page.keyboard.press(combo)
                break
            except Exception:
                continue
        try:
            page.keyboard.press("Backspace")
        except Exception:
            pass
        self._type_prompt_with_inline_references(page, input_locator, prompt_text)
        time.sleep(0.2)

    def _typing_delay_ms(self) -> int:
        speed = float(self.cfg.get("typing_speed", 1.0) or 1.0)
        speed = max(0.5, min(2.0, speed))
        base = 34
        return max(8, int(base / speed))

    def _humanize_enabled(self) -> bool:
        return bool(self.cfg.get("humanize_typing", True))

    def _type_text_human_like(self, *, page, text: str) -> None:
        typo_pool = "abcdefghijklmnopqrstuvwxyz"
        typed_since_pause = 0
        base_delay_ms = self._typing_delay_ms()
        for ch in str(text or ""):
            if ch == "\n":
                page.keyboard.press("Shift+Enter")
                typed_since_pause = 0
                time.sleep(random.uniform(0.05, 0.16))
                continue

            delay = max(8, int(base_delay_ms * random.uniform(0.7, 1.5)))
            if self._humanize_enabled() and ch.isalpha() and random.random() < 0.015:
                typo = random.choice(typo_pool)
                if typo.lower() == ch.lower():
                    typo = "x"
                page.keyboard.type(typo, delay=delay)
                time.sleep(random.uniform(0.03, 0.12))
                page.keyboard.press("Backspace")
                time.sleep(random.uniform(0.02, 0.08))

            page.keyboard.type(ch, delay=delay)
            typed_since_pause += 1

            if not self._humanize_enabled():
                continue
            if ch in ",.;:)":
                time.sleep(random.uniform(0.04, 0.14))
                typed_since_pause = 0
                continue
            if ch == " " and typed_since_pause >= random.randint(6, 14):
                time.sleep(random.uniform(0.06, 0.22))
                typed_since_pause = 0
                continue
            if random.random() < 0.01:
                time.sleep(random.uniform(0.08, 0.28))

    def _resolve_download_dir(self) -> Path:
        raw = str(self.cfg.get("download_output_dir") or "").strip()
        path = Path(raw) if raw else (self.base_dir / "downloads")
        if not path.is_absolute():
            path = self.base_dir / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _download_quality(self, mode=None):
        mode = "image" if mode == "image" else "video"
        if mode == "image":
            val = str(self.cfg.get("download_image_quality", self.cfg.get("image_quality", "1K")) or "1K").strip().upper()
            return val if val in ("1K", "2K", "4K") else "1K"
        val = str(self.cfg.get("download_video_quality", self.cfg.get("video_quality", "1080P")) or "1080P").strip().upper()
        return val if val in ("720P", "1080P", "4K") else "1080P"

    def _download_more_candidates(self):
        return [
            "button[aria-label*='더보기' i]",
            "[role='button'][aria-label*='더보기' i]",
            "button[aria-label*='more' i]",
            "[role='button'][aria-label*='more' i]",
            "button[title*='more' i]",
            "button:has-text('...')",
            "button:has-text('⋮')",
        ]

    def _download_menu_candidates(self):
        return [
            "button:has-text('다운로드')",
            "[role='menuitem']:has-text('다운로드')",
            "[role='button']:has-text('다운로드')",
            "text=다운로드",
            "button:has-text('Download')",
            "[role='menuitem']:has-text('Download')",
            "text=Download",
        ]

    def _download_quality_candidates(self, quality: str):
        quality = str(quality or "").strip().upper()
        cands = [
            f"button:has-text('{quality}')",
            f"[role='menuitem']:has-text('{quality}')",
            f"[role='option']:has-text('{quality}')",
            f"text={quality}",
        ]
        seen = set()
        uniq = []
        for item in cands:
            if item not in seen:
                uniq.append(item)
                seen.add(item)
        return uniq

    def _download_search_input_candidates(self) -> list[str]:
        cands = [
            "input[placeholder*='검색' i]",
            "input[placeholder*='search' i]",
            "input[aria-label*='검색' i]",
            "input[aria-label*='search' i]",
            "input[type='search']",
            "input.quick-search-input",
            "input",
        ]
        seen = set()
        uniq = []
        for item in cands:
            if item not in seen:
                uniq.append(item)
                seen.add(item)
        return uniq

    def _resolve_download_search_input(self, page, timeout_sec: float = 1.8):
        end_ts = time.time() + max(0.8, float(timeout_sec))
        while time.time() < end_ts:
            best = None
            best_sel = ""
            best_score = float("-inf")
            for sel in self._download_search_input_candidates():
                try:
                    loc = page.locator(sel)
                    total = min(loc.count(), 40)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=250):
                            continue
                        box = cand.bounding_box()
                    except Exception:
                        continue
                    if not box:
                        continue
                    width = float(box.get("width") or 0.0)
                    height = float(box.get("height") or 0.0)
                    x = float(box.get("x") or 0.0)
                    y = float(box.get("y") or 0.0)
                    if width < 120.0 or height < 18.0:
                        continue
                    meta = self._locator_meta_text(cand)
                    score = 0.0
                    if any(key in meta for key in ("검색", "search")):
                        score += 900.0
                    if any(key in meta for key in ("asset", "에셋", "애셋")):
                        score -= 700.0
                    if any(key in meta for key in ("prompt", "프롬프트", "message", "무엇을 만들")):
                        score -= 1000.0
                    if 180.0 <= width <= 620.0:
                        score += 150.0
                    score -= y * 1.8
                    score -= abs(x - 320.0) * 0.08
                    if score > best_score:
                        best = cand
                        best_sel = sel
                        best_score = score
            if best is not None and best_score > -200.0:
                return best, best_sel
            time.sleep(0.10)
        return None, None

    def _set_download_search_tag(self, page, tag: str):
        search_input, search_sel = self._resolve_download_search_input(page, timeout_sec=1.8)
        if search_input is None:
            self._emit_action(f"다운로드 검색창 미탐지: {tag}")
            return False
        try:
            search_input.click(timeout=600)
        except Exception:
            try:
                search_input.focus(timeout=500)
            except Exception:
                self._emit_action(f"다운로드 검색창 포커스 실패: {tag}")
                return False
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        except Exception:
            pass
        try:
            search_input.fill(tag, timeout=700)
        except Exception:
            try:
                search_input.type(tag, delay=random.randint(25, 65), timeout=1400)
            except Exception:
                self._emit_action(f"다운로드 검색 입력 실패: {tag}")
                return False
        self._emit_log(f"🔎 다운로드 검색 입력: {tag} ({search_sel or '자동 탐색'})")
        self._emit_action(f"다운로드 검색 입력: {tag} ({search_sel or '자동 탐색'})")
        time.sleep(0.12)
        typed = self._normalize_reference_asset_tag(self._read_input_text(search_input))
        if typed != self._normalize_reference_asset_tag(tag):
            self._emit_action(f"다운로드 검색 입력 불일치: typed={typed or '-'} wanted={tag}")
            return False
        time.sleep(0.35)
        return True

    def _clear_download_search_tag(self, page) -> None:
        search_input, _ = self._resolve_download_search_input(page, timeout_sec=1.0)
        if search_input is None:
            return
        try:
            search_input.click(timeout=500)
        except Exception:
            try:
                search_input.focus(timeout=500)
            except Exception:
                return
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        except Exception:
            try:
                search_input.fill("", timeout=500)
            except Exception:
                return
        self._emit_action("다운로드 검색어 초기화")
        time.sleep(0.12)

    def _normalize_download_tag(self, tag) -> str:
        normalized = self._normalize_reference_asset_tag(tag)
        if normalized:
            return normalized
        return str(tag or "").strip().upper()

    def _normalize_download_search_text(self, text) -> str:
        return re.sub(r"\s+", "", str(text or "").strip()).upper()

    def _download_tag_patterns(self, tag) -> list[str]:
        normalized = self._normalize_download_tag(tag)
        compact = self._normalize_download_search_text(normalized)
        patterns = [compact] if compact else []
        match = re.match(r"^([A-Z]+)(0*)([1-9][0-9]*)$", compact)
        if match:
            prefix = match.group(1)
            number = str(int(match.group(3)))
            patterns.append(f"{prefix}{number}")
        return list(dict.fromkeys([x for x in patterns if x]))

    def _download_page_contains_tag(self, page, tag: str) -> bool:
        patterns = self._download_tag_patterns(tag)
        if not patterns:
            return False
        try:
            matched = page.evaluate(
                """(patterns) => {
                    const normalize = (value) => String(value || "").replace(/\\s+/g, "").toUpperCase();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 16 || r.height < 12) return false;
                        const st = window.getComputedStyle(el);
                        return st && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
                    };
                    const nodes = document.querySelectorAll("div, span, p, button, li, article, section, a");
                    for (const node of nodes) {
                        if (!isVisible(node)) continue;
                        const raw = (node.innerText || node.textContent || "").trim();
                        if (!raw || raw.length > 80) continue;
                        const normalized = normalize(raw);
                        if (!normalized) continue;
                        if (patterns.some((pattern) => normalized.includes(String(pattern || '').toUpperCase()))) {
                            return true;
                        }
                    }
                    return false;
                }""",
                patterns,
            )
        except Exception:
            return False
        return bool(matched)

    def _probe_download_search_result_state(self, page, tag: str):
        if not tag:
            return "pending", ""
        expected = str(tag or "").strip().lower()
        try:
            result = page.evaluate(
                """(payload) => {
                    const expected = String(payload.tag || "").trim().toLowerCase();
                    const emptyHits = [
                        "일치하는 결과 없음",
                        "선택한 항목과 일치하는 결과가 없습니다",
                        "no matching results",
                        "no results",
                    ];
                    const failurePrimary = [
                        "문제가 발생했습니다",
                        "정책 위반",
                        "google 정책",
                        "google policy",
                        "may violate google policy",
                    ];
                    const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
                    const isVisible = (el) => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width < 24 || rect.height < 16) return false;
                        const style = window.getComputedStyle(el);
                        return style && style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
                    };
                    const isInputLike = (el) => {
                        if (!el) return false;
                        const tag = String(el.tagName || "").toLowerCase();
                        if (tag === "input" || tag === "textarea") return true;
                        if (el.isContentEditable) return true;
                        const role = String(el.getAttribute("role") || "").toLowerCase();
                        return role === "textbox" || role === "searchbox";
                    };
                    const nodes = document.querySelectorAll("div, li, button, span, p, a, h1, h2, h3, article, section");
                    let emptyReason = "";
                    let failureReason = "";
                    let found = false;
                    for (const node of nodes) {
                        if (!isVisible(node)) continue;
                        if (isInputLike(node)) continue;
                        if (node.querySelector("input, textarea, [role='textbox'], [role='searchbox'], [contenteditable='true']")) continue;
                        const rect = node.getBoundingClientRect();
                        if (rect.top < 0 || rect.top > window.innerHeight * 0.94) continue;
                        if (rect.left < 0 || rect.left > window.innerWidth * 0.96) continue;
                        if (rect.width > window.innerWidth * 0.98 && rect.height > window.innerHeight * 0.80) continue;
                        const text = clean(node.innerText || "");
                        if (!text || text.length > 260) continue;
                        const lower = text.toLowerCase();
                        if (!emptyReason && emptyHits.some((hit) => lower.includes(hit))) {
                            emptyReason = text.slice(0, 180);
                        }
                        if (!failureReason) {
                            const hasFailWord = lower.includes("실패");
                            const hasFailureBody = failurePrimary.some((hit) => lower.includes(hit));
                            if (hasFailureBody || (hasFailWord && (lower.includes("문제가 발생했습니다") || lower.includes("정책 위반")))) {
                                failureReason = text.slice(0, 180);
                            }
                        }
                        if (!found) {
                            const lines = text.split(/\\n+/).map(clean).filter(Boolean);
                            if (lines.some((line) => line.toLowerCase() === expected)) {
                                found = true;
                            }
                        }
                    }
                    if (emptyReason) return {state: "empty", reason: emptyReason};
                    if (failureReason) return {state: "failure", reason: failureReason};
                    if (found) return {state: "found", reason: expected};
                    return {state: "pending", reason: ""};
                }""",
                {"tag": expected},
            ) or {}
            state = str((result or {}).get("state", "") or "").strip().lower()
            reason = str((result or {}).get("reason", "") or "").strip()
            if state in ("empty", "failure", "found"):
                return state, reason
        except Exception:
            pass
        return "pending", ""

    def _download_search_input_matches_tag(self, search_loc, tag):
        if search_loc is None:
            return False
        expected = self._normalize_download_search_text(tag)
        if not expected:
            return False
        try:
            current = self._normalize_download_search_text(self._read_input_text(search_loc))
            return bool(current) and current == expected
        except Exception:
            return False

    def _download_card_candidates(self):
        cands = [
            "article",
            "[role='listitem']",
            "div[class*='card' i]",
            "div[class*='tile' i]",
            "div[data-testid*='card' i]",
            "li",
            "section",
        ]
        seen = set()
        uniq = []
        for item in cands:
            if item not in seen:
                uniq.append(item)
                seen.add(item)
        return uniq

    def _download_card_matches_tag(self, card_loc, tag):
        if card_loc is None:
            return False, ""
        meta = self._normalize_download_search_text(self._locator_meta_text(card_loc))
        if not meta:
            return False, ""
        for pattern in self._download_tag_patterns(tag):
            if pattern and pattern in meta:
                return True, meta
        return False, meta

    def _reject_download_card_candidate(self, locator, selector=None):
        meta = self._normalize_download_search_text(self._locator_meta_text(locator))
        if not meta:
            return False
        noisy_tokens = (
            "CHECK_CIRCLE",
            "업스케일링이완료",
            "업스케일링",
            "완료되었습니다",
            "닫기",
            "CLOSE",
            "SNACKBAR",
            "TOAST",
            "ALERT",
            "NOTICE",
            "알림",
            "완료",
        )
        return any(token in meta for token in noisy_tokens)

    def _score_download_card_candidate(self, page, locator, selector, tag):
        if locator is None:
            return float("-inf"), False, ""
        try:
            box = locator.bounding_box()
        except Exception:
            box = None
        if not box:
            return float("-inf"), False, ""
        width = float(box.get("width") or 0.0)
        height = float(box.get("height") or 0.0)
        x = float(box.get("x") or 0.0)
        y = float(box.get("y") or 0.0)
        if width < 150.0 or height < 90.0:
            return float("-inf"), False, ""
        viewport_w = 1600.0
        viewport_h = 900.0
        try:
            viewport_w = float(page.evaluate("window.innerWidth") or viewport_w)
            viewport_h = float(page.evaluate("window.innerHeight") or viewport_h)
        except Exception:
            pass
        area = width * height
        if width >= (viewport_w * 0.92) and height >= (viewport_h * 0.58):
            return float("-inf"), False, ""
        if area > (viewport_w * viewport_h * 0.52):
            return float("-inf"), False, ""
        meta = self._normalize_download_search_text(self._locator_meta_text(locator))
        matched, _ = self._download_card_matches_tag(locator, tag)
        score = 0.0
        if matched:
            score += 5000.0
        try:
            detail = locator.evaluate(
                """(el) => {
                    const media = el.querySelectorAll ? el.querySelectorAll("img, video, canvas").length : 0;
                    const buttons = el.querySelectorAll ? el.querySelectorAll("button, [role='button']").length : 0;
                    const cls = String(el.className || "").toLowerCase();
                    const role = String(el.getAttribute("role") || "").toLowerCase();
                    const tag = String(el.tagName || "").toLowerCase();
                    return { media, buttons, cls, role, tag };
                }"""
            ) or {}
        except Exception:
            detail = {}
        media_count = int(detail.get("media") or 0)
        button_count = int(detail.get("buttons") or 0)
        cls = str(detail.get("cls") or "")
        role = str(detail.get("role") or "")
        tag_name = str(detail.get("tag") or "")
        selector_l = str(selector or "").lower()
        if media_count > 0:
            score += 320.0
        if button_count > 0:
            score += 70.0
        if tag_name == "article":
            score += 140.0
        if role == "listitem":
            score += 110.0
        if any(token in cls for token in ("card", "tile", "result", "item", "media")):
            score += 120.0
        if any(token in selector_l for token in ("article", "listitem", "card", "tile", "result")):
            score += 80.0
        if 180.0 <= width <= 760.0:
            score += 120.0
        elif width > 980.0:
            score -= 260.0
        if 120.0 <= height <= 620.0:
            score += 120.0
        elif height > 760.0:
            score -= 260.0
        score -= (y * 0.38)
        score -= (x * 0.08)
        if any(token in meta for token in ("DOWNLOAD", "다운로드", "UPSCALE", "업스케일", "TOAST", "ALERT", "NOTICE")):
            score -= 800.0
        if any(token in meta for token in ("FILTER", "필터", "SEARCH", "검색")):
            score -= 500.0
        return score, matched, meta

    def _resolve_download_card_for_tag(self, page, tag, timeout_sec=6):
        end_ts = time.time() + max(1, timeout_sec)
        best_fallback = None
        best_fallback_sel = None
        best_fallback_meta = ""
        best_fallback_score = float("-inf")
        while time.time() < end_ts:
            best_match = None
            best_match_sel = None
            best_match_meta = ""
            best_match_score = float("-inf")
            for sel in self._download_card_candidates():
                try:
                    loc = page.locator(sel)
                    total = min(loc.count(), 40)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=700):
                            continue
                    except Exception:
                        continue
                    if self._reject_download_card_candidate(cand, sel):
                        continue
                    score, matched, meta = self._score_download_card_candidate(page, cand, sel, tag)
                    if score == float("-inf"):
                        continue
                    if matched and score > best_match_score:
                        best_match = cand
                        best_match_sel = sel
                        best_match_meta = meta
                        best_match_score = score
                    if score > best_fallback_score:
                        best_fallback = cand
                        best_fallback_sel = sel
                        best_fallback_meta = meta
                        best_fallback_score = score
            if best_match is not None:
                return best_match, best_match_sel, best_match_meta
            time.sleep(0.35)
        return best_fallback, best_fallback_sel, best_fallback_meta

    def _count_visible_media_tiles(self, page):
        try:
            return int(page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 160 || r.height < 90) return false;
                        const st = window.getComputedStyle(el);
                        if (!st) return false;
                        return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
                    };
                    let count = 0;
                    for (const el of document.querySelectorAll("video, img, canvas")) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.top < 70) continue;
                        count += 1;
                    }
                    return count;
                }"""
            ) or 0)
        except Exception:
            return 0

    def _find_first_media_tile_box(self, page):
        try:
            return page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 160 || r.height < 90) return false;
                        const st = window.getComputedStyle(el);
                        if (!st) return false;
                        return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
                    };
                    const candidates = Array.from(document.querySelectorAll("video, img, canvas"));
                    const boxes = [];
                    for (const el of candidates) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.top < 70) continue;
                        boxes.push({x:r.left, y:r.top, width:r.width, height:r.height});
                    }
                    boxes.sort((a,b) => (a.y - b.y) || (a.x - b.x));
                    return boxes.length ? boxes[0] : null;
                }"""
            )
        except Exception:
            return None

    def _find_card_box_for_tag(self, page, tag: str):
        try:
            return page.evaluate(
                """(payload) => {
                    const wanted = String(payload.tag || "").replace(/\\s+/g, "").toUpperCase();
                    if (!wanted) return null;
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 20 || r.height < 12) return false;
                        const st = window.getComputedStyle(el);
                        return st && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
                    };
                    const normalize = (value) => String(value || "").replace(/\\s+/g, "").toUpperCase();
                    let best = null;
                    let bestScore = -1e12;
                    const nodes = document.querySelectorAll("div, span, p, button, li, article, section, a");
                    for (const node of nodes) {
                        if (!isVisible(node)) continue;
                        const raw = (node.innerText || node.textContent || "").trim();
                        if (!raw || raw.length > 120) continue;
                        const normalized = normalize(raw);
                        if (!normalized.includes(wanted)) continue;
                        let host = node;
                        let depth = 0;
                        while (host.parentElement && depth < 6) {
                            const parent = host.parentElement;
                            if (!isVisible(parent)) break;
                            const media = parent.querySelector("img, video, canvas");
                            const rect = parent.getBoundingClientRect();
                            if (media && rect.width >= 180 && rect.height >= 100) {
                                host = parent;
                            }
                            depth += 1;
                        }
                        const r = host.getBoundingClientRect();
                        if (!r || r.width < 180 || r.height < 100) continue;
                        const score = (r.width * r.height) - (r.top * 180) - Math.abs(r.left - 120) * 12;
                        if (score > bestScore) {
                            bestScore = score;
                            best = {x:r.left, y:r.top, width:r.width, height:r.height};
                        }
                    }
                    return best;
                }""",
                {"tag": tag},
            )
        except Exception:
            return None

    def _resolve_more_button_near_box(self, page, box):
        if not box:
            return None, None
        try:
            loc = page.locator("button, [role='button']")
            total = min(loc.count(), 250)
        except Exception:
            return None, None
        right_top_x = float(box["x"]) + float(box["width"]) - 18.0
        right_top_y = float(box["y"]) + 18.0
        best = None
        best_score = float("inf")
        for i in range(total):
            cand = loc.nth(i)
            try:
                if not cand.is_visible(timeout=500):
                    continue
                b = cand.bounding_box()
            except Exception:
                continue
            if not b:
                continue
            if b["x"] < box["x"] - 24 or b["x"] > (box["x"] + box["width"] + 24):
                continue
            if b["y"] < box["y"] - 30 or b["y"] > (box["y"] + min(140, box["height"] * 0.45)):
                continue
            if b["width"] > 110 or b["height"] > 70:
                continue
            cx = b["x"] + b["width"] / 2.0
            cy = b["y"] + b["height"] / 2.0
            score = abs(cx - right_top_x) + abs(cy - right_top_y)
            meta = self._locator_meta_text(cand)
            if any(x in meta for x in ("더보기", "more", "menu", "...", "⋮", "︙")):
                score -= 220.0
            if any(x in meta for x in ("play", "pause", "재생", "scene", "장면", "favorite", "즐겨찾기", "reuse", "재사용", "신고", "copy", "복사", "delete", "삭제")):
                score += 180.0
            if score < best_score:
                best_score = score
                best = cand
        if best is None:
            return None, None
        return best, "media-tile-top-right-button"

    def _wait_for_download_event(self, page, click_fn, *, timeout_sec: float = 30.0):
        downloads = []
        context_downloads = []

        def _on_download(download):
            downloads.append(download)

        def _on_context_download(download):
            context_downloads.append(download)

        page.on("download", _on_download)
        try:
            page.context.on("download", _on_context_download)
        except Exception:
            pass
        try:
            click_fn()
            deadline = time.time() + max(1.0, float(timeout_sec))
            while time.time() < deadline:
                if downloads:
                    return downloads[0]
                if context_downloads:
                    return context_downloads[0]
                time.sleep(0.2)
            raise RuntimeError("다운로드 이벤트를 시작하지 못했습니다.")
        finally:
            try:
                page.remove_listener("download", _on_download)
            except Exception:
                pass
            try:
                page.context.remove_listener("download", _on_context_download)
            except Exception:
                pass

    def _download_timeout_sec(self, quality: str) -> float:
        quality = str(quality or "").strip().upper()
        if quality == "4K":
            return 40.0
        if quality == "2K":
            return 30.0
        return 24.0

    def _project_url_hint(self) -> str:
        try:
            return str(self._current_project().get("url") or self.cfg.get("flow_site_url") or "").strip()
        except Exception:
            return str(self.cfg.get("flow_site_url") or "").strip()

    def _close_non_project_tabs(self, page) -> None:
        project_url = self._project_url_hint()
        try:
            pages = [p for p in list(page.context.pages or []) if p and (not p.is_closed())]
        except Exception:
            return
        keep = None
        for cand in pages:
            try:
                current_url = str(cand.url or "").strip()
            except Exception:
                current_url = ""
            if project_url and project_url in current_url:
                keep = cand
                break
        if keep is None:
            keep = page
        for cand in pages:
            if cand is keep:
                continue
            try:
                cand.close()
            except Exception:
                pass

    def _download_source_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        home_dir = self.base_dir.parent
        for cand in (
            self._resolve_download_dir(),
            home_dir / "Downloads",
            home_dir / "downloads",
            self.base_dir / "runtime" / "flow_worker_edge_profile" / "Default" / "Downloads",
            self.base_dir / "runtime" / "flow_worker_edge_profile" / "Downloads",
        ):
            try:
                if cand.exists() and cand.is_dir():
                    dirs.append(cand)
            except Exception:
                continue
        uniq: list[Path] = []
        seen = set()
        for item in dirs:
            key = str(item.resolve())
            if key not in seen:
                uniq.append(item)
                seen.add(key)
        return uniq

    def _ensure_download_behavior(self, page) -> None:
        target_dir = self._resolve_download_dir()
        last_error = ""
        try:
            session = page.context.new_cdp_session(page)
        except Exception as exc:
            self._emit_action(f"다운로드 경로 세션 생성 실패: {exc}")
            return
        for method, params in (
            ("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(target_dir)}),
            ("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(target_dir), "eventsEnabled": True}),
        ):
            try:
                session.send(method, params)
                self._emit_action(f"다운로드 경로 적용: {method} -> {target_dir}")
                return
            except Exception as exc:
                last_error = str(exc)
                continue
        if last_error:
            self._emit_action(f"다운로드 경로 적용 실패: {last_error}")

    def _scan_download_source_snapshot(self) -> dict[str, tuple[float, int]]:
        snapshot: dict[str, tuple[float, int]] = {}
        for folder in self._download_source_dirs():
            try:
                for path in folder.iterdir():
                    if not path.is_file():
                        continue
                    if path.suffix.lower() in {".crdownload", ".tmp", ".part"}:
                        continue
                    stat = path.stat()
                    snapshot[str(path)] = (float(stat.st_mtime), int(stat.st_size))
            except Exception:
                continue
        return snapshot

    def _wait_for_download_file_fallback(self, *, before_snapshot: dict[str, tuple[float, int]], started_at: float, timeout_sec: float = 18.0) -> Path | None:
        deadline = time.time() + max(2.0, float(timeout_sec))
        best_path = None
        stable_hits = 0
        last_sig = None
        while time.time() < deadline:
            candidates: list[tuple[float, int, Path]] = []
            for folder in self._download_source_dirs():
                try:
                    for path in folder.iterdir():
                        if not path.is_file():
                            continue
                        if path.suffix.lower() in {".crdownload", ".tmp", ".part"}:
                            continue
                        stat = path.stat()
                        key = str(path)
                        prev = before_snapshot.get(key)
                        mtime = float(stat.st_mtime)
                        size = int(stat.st_size)
                        if mtime + 0.01 < float(started_at):
                            continue
                        if prev and prev == (mtime, size):
                            continue
                        candidates.append((mtime, size, path))
                except Exception:
                    continue
            if candidates:
                candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
                best_path = candidates[0][2]
                sig = (str(best_path), candidates[0][1])
                if sig == last_sig:
                    stable_hits += 1
                else:
                    stable_hits = 0
                    last_sig = sig
                if stable_hits >= 2:
                    return best_path
            time.sleep(0.5)
        return best_path

    def _adopt_downloaded_file(self, source_path: Path, tag: str) -> str:
        output_dir = self._resolve_download_dir()
        ext = source_path.suffix.strip() or ".png"
        target = output_dir / f"{tag}{ext}"
        try:
            if source_path.resolve() == target.resolve():
                return target.name
        except Exception:
            pass
        if target.exists():
            stem = target.stem
            index = 2
            while True:
                cand = output_dir / f"{stem}_{index}{ext}"
                if not cand.exists():
                    target = cand
                    break
                index += 1
        try:
            source_path.replace(target)
        except Exception:
            import shutil

            shutil.copy2(source_path, target)
        return target.name

    def _save_download_file(self, download, tag: str) -> str:
        output_dir = self._resolve_download_dir()
        suggested = ""
        try:
            suggested = str(download.suggested_filename or "").strip()
        except Exception:
            suggested = ""
        filename = suggested or f"{tag}.png"
        target = output_dir / filename
        if target.exists():
            stem = target.stem
            suffix = target.suffix or ".png"
            index = 2
            while True:
                candidate = output_dir / f"{stem}_{index}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                index += 1
        download.save_as(str(target))
        return target.name

    def _download_image_for_tag(self, page, tag: str, quality: str, *, log: LogFn) -> str:
        self._ensure_download_behavior(page)
        before_snapshot = self._scan_download_source_snapshot()
        search_fallback_used = False
        card_box = None
        card_loc = None
        tile_count = 0
        if not self._download_page_contains_tag(page, tag):
            self._emit_log(f"🔎 카드 직접 탐지 실패, 다운로드 검색으로 전환: {tag}")
            self._emit_action(f"카드 직접 탐지 실패 -> 다운로드 검색 전환: {tag}")
            if self._set_download_search_tag(page, tag):
                search_fallback_used = True
        deadline = time.time() + max(12.0, self._download_timeout_sec(quality))
        while time.time() < deadline:
            tile_count = self._count_visible_media_tiles(page)
            result_state, result_reason = self._probe_download_search_result_state(page, tag)
            if result_state == "empty":
                raise RuntimeError(f"검색 결과에 {tag} 항목이 없습니다.")
            if result_state == "failure":
                raise RuntimeError(f"{tag} 검색 결과가 실패 상태입니다. ({result_reason or '실패'})")
            card_loc, _, card_meta = self._resolve_download_card_for_tag(page, tag, timeout_sec=1.0)
            if card_loc is not None:
                matched, _ = self._download_card_matches_tag(card_loc, tag)
                page_has_tag = self._download_page_contains_tag(page, tag)
                if matched or page_has_tag:
                    try:
                        card_box = card_loc.bounding_box()
                    except Exception:
                        card_box = None
                    if card_box:
                        if not matched:
                            self._emit_action(f"다운로드 카드 태그 직접일치 없음, 페이지 태그 기준 사용: {tag}")
                        break
            if (not card_box) and tile_count > 0 and search_fallback_used:
                card_box = self._find_first_media_tile_box(page)
                if card_box:
                    self._emit_action(f"다운로드 검색 결과 첫 타일 사용: {tag}")
                    break
            time.sleep(0.35)
        if not card_box:
            raise RuntimeError(f"{tag} 카드 위치를 찾지 못했습니다.")
        if search_fallback_used:
            self._emit_log(f"ℹ️ 다운로드는 검색 결과 첫 타일 기준으로 진행합니다: {tag}")

        page.mouse.move(float(card_box["x"]) + float(card_box["width"]) * 0.85, float(card_box["y"]) + 20.0, steps=8)
        time.sleep(0.25)
        more_loc, _ = self._resolve_more_button_near_box(page, card_box)
        if more_loc is None:
            more_loc, _ = self._resolve_best_locator(page, self._download_more_candidates(), timeout_ms=1200, prefer_enabled=False)
        if more_loc is None:
            raise RuntimeError("다운로드 더보기 버튼을 찾지 못했습니다.")

        self._emit_action(f"다운로드 더보기 클릭 시도: {tag}")
        self._click_locator(page, more_loc)
        time.sleep(0.35)
        download_loc, _ = self._resolve_best_locator(page, self._download_menu_candidates(), timeout_ms=1400, prefer_enabled=False)
        if download_loc is None:
            raise RuntimeError("다운로드 메뉴를 찾지 못했습니다.")
        self._emit_action(f"다운로드 메뉴 감지: {tag}")

        def _click_download_path():
            self._emit_action(f"다운로드 메뉴 클릭: {tag}")
            self._click_locator(page, download_loc)
            time.sleep(0.35)
            quality_loc, _ = self._resolve_best_locator(page, self._download_quality_candidates(quality), timeout_ms=1400, prefer_enabled=False)
            if quality_loc is not None:
                self._emit_action(f"다운로드 품질 클릭: {tag} | {quality}")
                self._click_locator(page, quality_loc)
            else:
                self._emit_action(f"다운로드 품질 미탐지: {tag} | {quality}")
        started_at = time.time()
        try:
            download = self._wait_for_download_event(page, _click_download_path, timeout_sec=self._download_timeout_sec(quality))
            saved_name = self._save_download_file(download, tag)
            log(f"다운로드 완료: {tag} -> {saved_name}")
            return saved_name
        except Exception as exc:
            self._emit_action(f"다운로드 이벤트 미감지, 파일 폴백 확인: {tag}")
            fallback_file = self._wait_for_download_file_fallback(
                before_snapshot=before_snapshot,
                started_at=started_at,
                timeout_sec=max(8.0, self._download_timeout_sec(quality)),
            )
            if fallback_file is not None:
                saved_name = self._adopt_downloaded_file(fallback_file, tag)
                log(f"다운로드 폴백 완료: {tag} -> {saved_name}")
                return saved_name
            raise exc
        finally:
            if search_fallback_used:
                self._clear_download_search_tag(page)
            self._close_non_project_tabs(page)

    def _submit_candidates(self) -> list[str]:
        cands = []
        cands.extend(self._normalize_candidate_list(self.cfg.get("submit_selector", "")))
        cands.extend(
            [
                "button[type='submit']",
                "button[aria-label*='generate' i]",
                "button[aria-label*='생성' i]",
                "button[aria-label*='보내' i]",
                "button[aria-label*='send' i]",
                "button[aria-label*='submit' i]",
                "[role='button'][aria-label*='생성' i]",
                "[role='button'][aria-label*='보내' i]",
                "[role='button'][aria-label*='send' i]",
                "button:has-text('Generate')",
                "button:has-text('생성')",
                "button:has-text('보내기')",
                "button:has-text('Create')",
            ]
        )
        seen = set()
        uniq = []
        for item in cands:
            if item not in seen:
                uniq.append(item)
                seen.add(item)
        return uniq

    def _resolve_submit_by_geometry(self, page, input_locator, timeout_ms: int = 1200):
        if input_locator is None:
            return None
        try:
            ib = input_locator.bounding_box()
        except Exception:
            ib = None
        if not ib:
            return None
        ix = ib["x"]
        iy = ib["y"]
        iw = ib["width"]
        ih = ib["height"]
        input_cy = iy + ih / 2.0
        input_right = ix + iw
        best = None
        best_score = float("inf")
        try:
            loc = page.locator("button, [role='button']")
            total = loc.count()
        except Exception:
            return None
        for i in range(min(total, 250)):
            cand = loc.nth(i)
            try:
                if not cand.is_visible(timeout=timeout_ms):
                    continue
            except Exception:
                continue
            try:
                if not cand.is_enabled(timeout=200):
                    continue
            except Exception:
                pass
            try:
                box = cand.bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            if box["width"] < 20 or box["height"] < 20 or box["width"] > 84 or box["height"] > 84:
                continue
            cx = box["x"] + box["width"] / 2.0
            cy = box["y"] + box["height"] / 2.0
            if cy < (iy - 36) or cy > (iy + ih + 36):
                continue
            if cx < (ix + iw * 0.82) or cx > (input_right + 110):
                continue
            score = abs(cy - input_cy) * 6.0
            score += abs(cx - (input_right + 26.0)) * 2.8
            score += abs(float(box["width"]) - 46.0) * 1.2
            score += abs(float(box["height"]) - 46.0) * 1.2
            meta = self._locator_meta_text(cand)
            if any(x in meta for x in ("애셋", "에셋", "asset", "검색", "search", "업로드", "upload", "이미지", "영상", "video", "image", "nano", "banana", "x2", "x3", "x4", "모델", "model", "메뉴", "설정", "help", "프로젝트")):
                continue
            if any(x in meta for x in ("생성", "generate", "send", "보내")):
                score -= 350.0
            if any(x in meta for x in ("arrow", "forward", "submit")):
                score -= 200.0
            if score < best_score:
                best = cand
                best_score = score
        return best

    def _resolve_best_locator(self, page, candidates, near_locator=None, timeout_ms: int = 1200, prefer_enabled: bool = True, reject_fn=None):
        near_cx = None
        near_cy = None
        if near_locator is not None:
            try:
                nb = near_locator.bounding_box()
                if nb:
                    near_cx = nb["x"] + nb["width"] / 2.0
                    near_cy = nb["y"] + nb["height"] / 2.0
            except Exception:
                pass
        best = None
        best_selector = None
        best_score = float("inf")
        containers = [page]
        try:
            containers.extend(fr for fr in page.frames if fr != page.main_frame)
        except Exception:
            pass
        for container in containers:
            for sel in candidates:
                try:
                    loc = container.locator(sel)
                    total = loc.count()
                except Exception:
                    continue
                for i in range(min(total, 20)):
                    cand = loc.nth(i)
                    try:
                        if not cand.is_visible(timeout=timeout_ms):
                            continue
                    except Exception:
                        continue
                    if reject_fn is not None:
                        try:
                            if reject_fn(cand, sel):
                                continue
                        except Exception:
                            pass
                    try:
                        box = cand.bounding_box()
                    except Exception:
                        box = None
                    if not box:
                        continue
                    score = 0.0
                    try:
                        enabled = cand.is_enabled(timeout=300)
                    except Exception:
                        enabled = True
                    if not enabled and prefer_enabled:
                        score += 1200.0
                    if box["width"] < 20 or box["height"] < 12:
                        score += 3000.0
                    if near_cx is not None and near_cy is not None:
                        cx = box["x"] + box["width"] / 2.0
                        cy = box["y"] + box["height"] / 2.0
                        score += ((cx - near_cx) ** 2 + (cy - near_cy) ** 2) ** 0.5
                    else:
                        score += float(i)
                    if score < best_score:
                        best = cand
                        best_selector = sel
                        best_score = score
        return best, best_selector

    def _resolve_submit_locator_for_input(self, page, input_locator, timeout_ms: int = 1200):
        input_box = None
        try:
            input_box = input_locator.bounding_box() if input_locator is not None else None
        except Exception:
            input_box = None

        def _reject_submit_candidate(cand, _sel):
            if input_box is None:
                return False
            try:
                box = cand.bounding_box()
            except Exception:
                box = None
            if not box:
                return True
            try:
                ix = float(input_box["x"])
                iy = float(input_box["y"])
                iw = float(input_box["width"])
                ih = float(input_box["height"])
                bw = float(box["width"])
                bh = float(box["height"])
                cx = float(box["x"]) + float(box["width"]) / 2.0
                cy = float(box["y"]) + float(box["height"]) / 2.0
            except Exception:
                return True
            if bw < 20.0 or bh < 20.0 or bw > 84.0 or bh > 84.0:
                return True
            if cy < (iy - 36.0) or cy > (iy + ih + 36.0):
                return True
            if cx < (ix + iw * 0.82) or cx > (ix + iw + 110.0):
                return True
            meta = self._locator_meta_text(cand)
            if any(x in meta for x in ("애셋", "에셋", "asset", "검색", "search", "업로드", "upload", "이미지", "영상", "video", "image", "nano", "banana", "x2", "x3", "x4", "모델", "model", "메뉴", "설정", "도움", "help", "프로젝트")):
                return True
            return False

        submit_loc = self._resolve_submit_by_geometry(page, input_locator, timeout_ms=timeout_ms)
        submit_sel = "geometry" if submit_loc is not None else None
        if submit_loc is None:
            submit_loc, submit_sel = self._resolve_best_locator(
                page,
                self._submit_candidates(),
                near_locator=input_locator,
                timeout_ms=timeout_ms,
                prefer_enabled=False,
                reject_fn=_reject_submit_candidate,
            )
        return submit_loc, submit_sel or ""

    def _capture_submit_state(self, submit_locator):
        state = {"visible": False, "enabled": None, "meta": ""}
        if submit_locator is None:
            return state
        try:
            state["visible"] = bool(submit_locator.is_visible(timeout=250))
        except Exception:
            state["visible"] = False
        try:
            state["enabled"] = bool(submit_locator.is_enabled(timeout=250))
        except Exception:
            state["enabled"] = None
        try:
            state["meta"] = str(self._locator_meta_text(submit_locator) or "").strip().lower()
        except Exception:
            state["meta"] = ""
        return state

    def _is_generation_indicator_visible(self, page) -> bool:
        indicators = [
            "button:has-text('생성 중')",
            "button:has-text('처리 중')",
            "button:has-text('중지')",
            "button:has-text('취소')",
            "button:has-text('Stop')",
            "button:has-text('Cancel')",
            "text=/생성 중|Generating/i",
        ]
        for sel in indicators:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=250):
                    return True
            except Exception:
                continue
        return False

    def _confirm_submission_started(
        self,
        page,
        input_locator,
        before_text,
        *,
        timeout_sec: int = 12,
        submit_locator=None,
        submit_before_state=None,
        indicator_before: bool = False,
    ):
        end_ts = time.time() + max(2, timeout_sec)
        before_text = (before_text or "").strip()
        min_shrunk_len = max(2, int(len(before_text) * 0.25)) if before_text else 2
        submit_before_state = dict(submit_before_state or {})
        before_enabled = submit_before_state.get("enabled")
        before_meta = str(submit_before_state.get("meta") or "").strip().lower()
        while time.time() < end_ts:
            current_submit_state = self._capture_submit_state(submit_locator) if submit_locator is not None else {}
            current_meta = str(current_submit_state.get("meta") or "").strip().lower()
            if submit_locator is not None:
                if (before_enabled is True) and (current_submit_state.get("enabled") is False):
                    return True, "submit_disabled"
                if current_meta and current_meta != before_meta and any(x in current_meta for x in ("중지", "취소", "stop", "cancel", "generating", "생성 중", "processing", "처리 중")):
                    return True, "submit_changed"
            if self._is_generation_indicator_visible(page) and (not indicator_before):
                return True, "generation_indicator"
            current = self._read_input_text(input_locator)
            if before_text and current != before_text:
                if len(current) <= min_shrunk_len:
                    return True, "input_cleared"
                if len(current) < max(4, int(len(before_text) * 0.55)):
                    return True, "input_shrunk"
            time.sleep(0.5)
        return False, "timeout"

    def _filter_items(self, items: list[PromptBlock]) -> list[PromptBlock]:
        mode = str(self.cfg.get("number_mode") or "all").strip().lower()
        if mode == "manual":
            wanted = set(self._parse_manual_numbers(str(self.cfg.get("manual_numbers") or "")))
            return [item for item in items if item.number in wanted]
        if mode == "range":
            start = int(self.cfg.get("start_number", 1) or 1)
            end = int(self.cfg.get("end_number", start) or start)
            lo, hi = min(start, end), max(start, end)
            return [item for item in items if lo <= item.number <= hi]
        return items

    def _parse_manual_numbers(self, raw: str) -> list[int]:
        result: set[int] = set()
        for part in str(raw or "").replace(" ", "").split(","):
            if not part:
                continue
            token = part.upper()
            while token and token[0].isalpha():
                token = token[1:]
            if "-" in token:
                left, right = token.split("-", 1)
                if left.isdigit() and right.isdigit():
                    lo, hi = sorted((int(left), int(right)))
                    result.update(range(lo, hi + 1))
                continue
            if token.isdigit():
                result.add(int(token))
        return sorted(result)

    def _selection_summary(self, items: list[PromptBlock], media_mode: str) -> str:
        if not items:
            return "선택된 작업 없음"
        numbers = [item.number for item in items]
        prefix = "V" if media_mode == "video" else "S"
        compact = compress_numbers(numbers, prefix=prefix)
        return f"{len(items)}개 선택: {compact}"
