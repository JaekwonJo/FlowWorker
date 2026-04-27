from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .browser import BrowserManager
from .prompt_parser import PromptBlock, compress_numbers, load_prompt_blocks


LogFn = Callable[[str], None]
StatusFn = Callable[[str], None]
QueueFn = Callable[[int, str, str, str], None]
StopFn = Callable[[], bool]
PauseFn = Callable[[], bool]


@dataclass
class RunPlan:
    items: list[PromptBlock]
    selection_summary: str


class FlowAutomationEngine:
    def __init__(self, base_dir: Path, cfg: dict):
        self.base_dir = Path(base_dir)
        self.cfg = cfg

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
        update_queue: QueueFn,
        should_stop: StopFn,
        is_paused: PauseFn,
        browser: BrowserManager,
    ) -> None:
        if not plan.items:
            set_status("선택된 작업 없음")
            return

        media_mode = str(self.cfg.get("media_mode") or "image").strip().lower()
        if media_mode != "image":
            set_status("비디오 모드는 다음 단계 예정")
            log("비디오 모드는 아직 연결 전입니다. 이번 단계는 이미지 모드 핵심 자동화만 먼저 붙였습니다.")
            return

        project = self._current_project()
        project_url = str(project.get("url") or self.cfg.get("flow_site_url") or "").strip()
        if not project_url:
            raise RuntimeError("Flow 프로젝트 URL이 비어 있습니다.")

        set_status("브라우저 준비 중")
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
        if not self._wait_until_input_visible(page, input_selector_hint, timeout_sec=18):
            self._try_open_new_project_if_needed(page, input_selector_hint, log=log)
        input_locator = self._wait_for_prompt_input(page, input_selector_hint)
        if input_locator is None:
            raise RuntimeError("Flow 입력창을 찾지 못했습니다. 페이지가 완전히 열린 뒤 다시 시도해주세요.")

        self._apply_image_preset(page, input_locator, log=log)
        set_status("이미지 모드 준비 완료")

        generate_wait = max(0.5, float(self.cfg.get("generate_wait_seconds", 10.0) or 10.0))
        next_wait = max(0.0, float(self.cfg.get("next_prompt_wait_seconds", 7.0) or 7.0))

        for item in plan.items:
            if should_stop():
                set_status("중지됨")
                log("사용자 중지 요청으로 작업을 멈췄습니다.")
                return
            self._wait_if_paused(is_paused=is_paused, should_stop=should_stop, set_status=set_status)
            if should_stop():
                set_status("중지됨")
                return

            try:
                update_queue(item.number, "running", f"{item.tag} 프롬프트 입력 중", "")
                set_status(f"{item.tag} 입력 중")
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
                self._sleep_with_control(generate_wait, should_stop=should_stop, is_paused=is_paused, set_status=set_status)
                if should_stop():
                    set_status("중지됨")
                    return

                update_queue(item.number, "success", "이미지 생성 요청 제출 완료", "")
                set_status(f"{item.tag} 다음 작업 대기")
                if next_wait > 0:
                    self._sleep_with_control(next_wait, should_stop=should_stop, is_paused=is_paused, set_status=set_status)
            except Exception as exc:
                update_queue(item.number, "failed", str(exc), "")
                log(f"실패: {item.tag} | {exc}")

        set_status("이미지 모드 1차 완료")
        log("이미지 모드 1차: 프롬프트 제출 자동화까지 완료했습니다.")

    def _wait_if_paused(self, *, is_paused: PauseFn, should_stop: StopFn, set_status: StatusFn) -> None:
        announced = False
        while is_paused() and not should_stop():
            if not announced:
                set_status("일시정지")
                announced = True
            time.sleep(0.2)

    def _sleep_with_control(self, seconds: float, *, should_stop: StopFn, is_paused: PauseFn, set_status: StatusFn) -> None:
        end_at = time.time() + max(0.0, float(seconds))
        while time.time() < end_at:
            if should_stop():
                return
            if is_paused():
                self._wait_if_paused(is_paused=is_paused, should_stop=should_stop, set_status=set_status)
                end_at = time.time() + max(0.0, end_at - time.time())
            time.sleep(0.15)

    def _current_project(self) -> dict:
        profiles = list(self.cfg.get("project_profiles") or [])
        idx = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1)) if profiles else 0
        return profiles[idx] if profiles else {"name": "기본 프로젝트", "url": self.cfg.get("flow_site_url", "")}

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
        self._type_text_human_like(page=page, text=prompt_text)
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
