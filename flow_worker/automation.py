from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .human_actor import HumanActor
from .prompt_parser import PromptBlock, compress_numbers, load_prompt_blocks
from .video_frame_tools import LastFrameExtractError, extract_last_frame, suggested_next_frame_path_for_tag


LogFn = Callable[[str], None]
StatusFn = Callable[[str], None]
QueueFn = Callable[[int, str, str, str], None]
ProgressFn = Callable[[int, int], None]
StopFn = Callable[[], bool]
PauseFn = Callable[[], bool]


@dataclass
class RunPlan:
    items: list[PromptBlock]
    selection_summary: str
    image_count: int = 0
    video_count: int = 0
    routed_count: int = 0


class FlowAutomationEngine:
    def __init__(self, base_dir: Path, cfg: dict, browser=None) -> None:
        self.base_dir = Path(base_dir)
        self.cfg = cfg
        self.browser = browser
        self.page = None
        self._action_log_path: Path | None = None
        self.actor = HumanActor(action_logger=self._action_log)
        self.has_inline_prompt_refs = False

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
        if media_mode == "video":
            selected = [item for item in selected if item.media_mode == "video"]
        else:
            selected = [item for item in selected if item.media_mode == "image"]
        image_count = sum(1 for item in selected if item.media_mode == "image")
        video_count = sum(1 for item in selected if item.media_mode == "video")
        routed_count = sum(1 for item in selected if item.route_end_tag)
        return RunPlan(
            items=selected,
            selection_summary=self._selection_summary(selected, media_mode),
            image_count=image_count,
            video_count=video_count,
            routed_count=routed_count,
        )

    def run(
        self,
        *,
        plan: RunPlan,
        log: LogFn,
        set_status: StatusFn,
        update_queue: QueueFn,
        update_progress: ProgressFn,
        should_stop: StopFn,
        is_paused: PauseFn,
    ) -> None:
        if not plan.items:
            set_status("선택된 작업 없음")
            return
        self._open_action_log(log)
        media_mode = str(self.cfg.get("media_mode") or "image").strip().lower()
        set_status("브라우저 준비 중")
        log(f"🧩 Flow Worker 독립 실행 시작 | 모드={media_mode} | 선택={plan.selection_summary}")
        self._action_log(f"[{self._clock()}] 독립 실행 시작 | 모드={media_mode} | 선택={plan.selection_summary}")
        self.page = self._ensure_project_page(log)
        self.actor.set_page(self.page)
        self.actor.set_typing_speed_profile(str(self.cfg.get("typing_speed_profile") or "x5"))
        set_status("브라우저 준비 완료")
        completed = 0
        failed = 0
        total = len(plan.items)
        update_progress(completed, total)
        try:
            self._switch_media_mode(media_mode=media_mode, log=log)
        except Exception as exc:
            set_status("생성 옵션 정렬 실패")
            log(f"❌ 생성 옵션 사전 정렬 실패: {exc}")
            self._action_log(f"[{self._clock()}] 생성 옵션 사전 정렬 실패: {exc}")
            for item in plan.items:
                update_queue(item.number, "failed", f"생성 옵션 정렬 실패: {exc}", "")
            update_progress(total, total)
            return
        for item in plan.items:
            if should_stop():
                set_status("중지됨")
                log("⏹️ 사용자 중지 요청으로 작업을 멈췄습니다.")
                return
            self._wait_if_paused(set_status, is_paused, should_stop)
            queue_tag = item.route_start_tag if media_mode == "video" and item.route_start_tag else item.tag
            update_queue(item.number, "running", "입력 준비 중", "")
            set_status(f"{queue_tag} 입력 중")
            log(f"▶ 작업 시작: {queue_tag}")
            self._action_log(f"[{self._clock()}] 작업 시작: {queue_tag}")
            try:
                self.actor.randomize_persona()
                input_locator = self._resolve_prompt_input()
                self.actor.clear_input_field(input_locator, label="입력창")
                self._attach_config_reference_files(input_locator, log)
                if media_mode == "video":
                    self._type_video_prompt(item, input_locator, log)
                else:
                    self._type_prompt_with_inline_references(item.rendered_prompt, input_locator, log)
                typed_text = self._read_input_text(input_locator)
                if len(typed_text.strip()) < max(4, min(24, len(str(item.rendered_prompt or "").strip()) // 6)):
                    log("⚠️ 입력 확인 결과가 짧아 입력창을 다시 찾아 재입력합니다.")
                    input_locator = self._resolve_prompt_input()
                    self.actor.clear_input_field(input_locator, label="입력창")
                    self._attach_config_reference_files(input_locator, log)
                    if media_mode == "video":
                        self._type_video_prompt(item, input_locator, log)
                    else:
                        self._type_prompt_with_inline_references(item.rendered_prompt, input_locator, log)
                    typed_text = self._read_input_text(input_locator)
                if len(typed_text.strip()) < max(4, min(24, len(str(item.rendered_prompt or "").strip()) // 6)):
                    raise RuntimeError("프롬프트 입력이 실제 입력창에 반영되지 않았습니다.")
                update_queue(item.number, "waiting", "생성 대기 중", "")
                set_status(f"{queue_tag} 생성 중")
                self._submit_prompt(input_locator)
                wait_sec = max(3.0, float(self.cfg.get("generate_wait_seconds", 10.0) or 10.0))
                self._timed_wait(wait_sec, should_stop, is_paused, set_status, label=f"{queue_tag} 생성 대기")
                if media_mode == "video" and bool(self.cfg.get("video_auto_extend", False)):
                    update_queue(item.number, "waiting", "확장 준비 중", "")
                    set_status(f"{queue_tag} 확장 준비 중")
                    self._open_result_detail(queue_tag, log)
                    self._extend_current_video(log=log)
                    extend_wait = max(5.0, float(self.cfg.get("video_extend_wait_seconds", 75.0) or 75.0))
                    update_queue(item.number, "waiting", "확장 생성 대기 중", "")
                    self._timed_wait(extend_wait, should_stop, is_paused, set_status, label=f"{queue_tag} 확장 대기")
                file_name = ""
                try:
                    quality = self._download_quality(media_mode)
                    if media_mode == "video" and bool(self.cfg.get("video_auto_extend", False)):
                        file_name = self._download_current_detail_video(tag=queue_tag, quality=quality, log=log)
                    else:
                        file_name = self._download_result(tag=queue_tag, quality=quality, log=log)
                    update_queue(item.number, "success", "완료", file_name)
                    log(f"✅ 작업 완료: {queue_tag} | {file_name or '다운로드 없음'}")
                except Exception as exc:
                    update_queue(item.number, "failed", str(exc), "")
                    log(f"❌ 다운로드 실패: {queue_tag} | {exc}")
                    self._action_log(f"[{self._clock()}] 다운로드 실패: {queue_tag} | {exc}")
                    failed += 1
                    completed += 1
                    update_progress(completed, total)
                    continue
                completed += 1
                update_progress(completed, total)
                next_wait = max(0.0, float(self.cfg.get("next_prompt_wait_seconds", 7.0) or 7.0))
                if next_wait > 0 and completed < total:
                    update_queue(item.number, "success", f"완료 후 {int(round(next_wait))}초 대기", file_name)
                    self._timed_wait(next_wait, should_stop, is_paused, set_status, label=f"{queue_tag} 다음 작업 대기")
            except Exception as exc:
                update_queue(item.number, "failed", str(exc), "")
                failed += 1
                completed += 1
                update_progress(completed, total)
                log(f"❌ 작업 실패: {queue_tag} | {exc}")
                self._action_log(f"[{self._clock()}] 작업 실패: {queue_tag} | {exc}")
        if failed:
            set_status(f"작업 종료 - 실패 {failed}개")
            log(f"🏁 독립 실행 종료 | 실패 {failed}개")
        else:
            set_status("작업 완료")
            log("🏁 독립 실행 종료")
        self._action_log(f"[{self._clock()}] 독립 실행 종료")

    def extend_current_video_screen(
        self,
        *,
        log: LogFn,
        set_status: StatusFn,
        should_stop: StopFn | None = None,
        is_paused: PauseFn | None = None,
        tag: str = "extended",
    ) -> str:
        self._open_action_log(log)
        set_status("브라우저 준비 중")
        self.page = self._ensure_project_page(log)
        self.actor.set_page(self.page)
        self.actor.set_typing_speed_profile(str(self.cfg.get("typing_speed_profile") or "x5"))
        if should_stop and should_stop():
            set_status("중지됨")
            return ""
        if is_paused and should_stop:
            self._wait_if_paused(set_status, is_paused, should_stop)
        set_status("현재 영상 확장 중")
        self._extend_current_video(log=log)
        extend_wait = max(5.0, float(self.cfg.get("video_extend_wait_seconds", 75.0) or 75.0))
        if should_stop and is_paused:
            self._timed_wait(extend_wait, should_stop, is_paused, set_status, label="현재 영상 확장 대기")
        else:
            time.sleep(extend_wait)
        quality = self._download_quality("video")
        file_name = self._download_current_detail_video(tag=tag, quality=quality, log=log)
        set_status("현재 영상 확장 완료")
        return file_name

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

    def _ensure_project_page(self, log: LogFn):
        project_profiles = list(self.cfg.get("project_profiles") or [])
        index = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(project_profiles) - 1)) if project_profiles else 0
        project = project_profiles[index] if project_profiles else {}
        url = str(project.get("url") or self.cfg.get("flow_site_url") or "").strip()
        profile_dir = str(self.cfg.get("browser_profile_dir") or "").strip()
        attach_url = str(self.cfg.get("browser_attach_url") or "").strip()
        if not profile_dir:
            worker_index = int(self.cfg.get("worker_index", 1) or 1)
            profile_dir = f"runtime/flow_worker_edge_profile_{worker_index}"
        page = self.browser.ensure_page(
            url=url,
            profile_dir=str((self.base_dir / profile_dir).resolve()),
            attach_url=attach_url,
            window_cfg=self.cfg,
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        self.page = page
        if bool(self.cfg.get("flow_create_new_project_on_run", False)):
            self._create_new_flow_project_if_needed(log)
        try:
            self._action_log(f"[{self._clock()}] 브라우저 현재 URL: {page.url}")
            self._action_log(f"[{self._clock()}] 브라우저 현재 제목: {page.title()}")
        except Exception:
            pass
        log("🌐 브라우저 세션 재사용 완료")
        return page

    def _create_new_flow_project_if_needed(self, log: LogFn) -> None:
        if not self.page:
            return
        try:
            current_url = str(self.page.url or "")
        except Exception:
            current_url = ""
        if "/project/" in current_url:
            return
        log("🆕 Flow 홈에서 새 프로젝트 생성 시도")
        self._action_log(f"[{self._clock()}] 새 프로젝트 생성 시도 URL: {current_url}")
        button = self._resolve_new_project_button()
        if button is None:
            self._dump_new_project_candidates("새 프로젝트 버튼 탐지 실패")
            raise RuntimeError("Flow 홈에서 새 프로젝트 버튼을 찾지 못했습니다.")
        if not self._click_with_actor_fallback(button, "새 프로젝트"):
            button.click(timeout=2500, force=True)
        time.sleep(0.8)
        self._fill_new_project_name_if_available(log)
        confirm = self._resolve_project_create_confirm_button()
        if confirm is not None:
            if not self._click_with_actor_fallback(confirm, "새 프로젝트 생성 확인"):
                confirm.click(timeout=2500, force=True)
        deadline = time.time() + 20.0
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                if "/project/" in str(self.page.url or ""):
                    self._fill_new_project_name_if_available(log)
                    log("🆕 새 프로젝트 진입 완료")
                    self._action_log(f"[{self._clock()}] 새 프로젝트 진입 완료: {self.page.url}")
                    return
            except Exception:
                pass
            try:
                if self._resolve_prompt_input():
                    self._fill_new_project_name_if_available(log)
                    log("🆕 새 프로젝트 입력창 확인 완료")
                    self._action_log(f"[{self._clock()}] 새 프로젝트 입력창 확인 완료")
                    return
            except Exception:
                pass
        raise RuntimeError("새 프로젝트 진입을 확인하지 못했습니다.")

    def _fill_new_project_name_if_available(self, log: LogFn) -> None:
        project_name = str(self.cfg.get("flow_new_project_name") or "").strip()
        if not project_name or not self.page:
            return
        name_input = self._resolve_project_name_input()
        if name_input is None:
            self._action_log(f"[{self._clock()}] 새 프로젝트 이름 입력칸 미탐지: {project_name}")
            return
        try:
            name_input.click(timeout=2500, force=True)
            name_input.press("Control+A", timeout=1200)
            name_input.type(project_name, delay=18, timeout=10000)
            try:
                name_input.press("Enter", timeout=1200)
            except Exception:
                pass
            log(f"🆕 새 프로젝트 이름 입력: {project_name}")
            self._action_log(f"[{self._clock()}] 새 프로젝트 이름 입력: {project_name}")
            time.sleep(0.8)
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 새 프로젝트 이름 입력 실패: {exc}")

    def _resolve_project_name_input(self):
        if not self.page:
            return None
        marker = f"flow-worker-project-name-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        try:
            result = self.page.evaluate(
                """(marker) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 30 && r.height >= 14 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const textOf = (el) => [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("placeholder")||"", el.getAttribute("title")||"", el.getAttribute("name")||"", el.value||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("input,textarea,[contenteditable='true'],[role='textbox']"));
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = textOf(el);
                        if (r.y > 260 || r.width > 620 || r.height > 80) continue;
                        let score = 0;
                        if (/프로젝트|project|title|제목|name|이름/.test(meta)) score += 800;
                        if (/untitled|제목\\s*없|제목없는|새\\s*프로젝트/.test(meta)) score += 500;
                        if ((el.tagName || "").toLowerCase() === "input") score += 140;
                        if ((el.getAttribute("contenteditable") || "").toLowerCase() === "true") score += 120;
                        if (r.x < 260 && r.y < 180) score += 220;
                        if (/무엇을 만들|prompt|프롬프트|message|메시지|검색|search|asset|에셋|애셋/.test(meta)) score -= 1200;
                        rows.push({score, meta:meta.slice(0, 160), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a,b) => b.score - a.score);
                    if (!best || bestScore < 160) return {ok:false, rows:rows.slice(0, 8)};
                    document.querySelectorAll("[data-flow-worker-project-name]").forEach((el) => el.removeAttribute("data-flow-worker-project-name"));
                    best.setAttribute("data-flow-worker-project-name", marker);
                    return {ok:true, marker, rows:rows.slice(0, 8)};
                }""",
                marker,
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 프로젝트 이름 입력칸 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "project-name", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("프로젝트 이름 입력칸 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-project-name='{marker}']").first

    def _resolve_project_create_confirm_button(self):
        if not self.page:
            return None
        marker = f"flow-worker-create-project-confirm-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        try:
            result = self.page.evaluate(
                """(marker) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 36 && r.height >= 24 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],div,span"));
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = metaText(el);
                        if (r.width > 260 || r.height > 90) continue;
                        let score = 0;
                        if (/만들|생성|확인|create|start|시작|done|완료/.test(meta)) score += 700;
                        if (/프로젝트|project/.test(meta)) score += 220;
                        if ((el.tagName || "").toLowerCase() === "button") score += 160;
                        if ((el.getAttribute("role") || "").toLowerCase() === "button") score += 120;
                        if (r.y > (window.innerHeight || 900) * 0.35) score += 80;
                        if (/취소|cancel|닫기|close|검색|search|삭제|delete/.test(meta)) score -= 900;
                        rows.push({score, meta:meta.slice(0, 160), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a,b) => b.score - a.score);
                    if (!best || bestScore < 300) return {ok:false, rows:rows.slice(0, 8)};
                    document.querySelectorAll("[data-flow-worker-create-project-confirm]").forEach((el) => el.removeAttribute("data-flow-worker-create-project-confirm"));
                    best.setAttribute("data-flow-worker-create-project-confirm", marker);
                    return {ok:true, marker, rows:rows.slice(0, 8)};
                }""",
                marker,
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 새 프로젝트 생성 확인 버튼 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "create-project-confirm", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("새 프로젝트 생성 확인 버튼 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-create-project-confirm='{marker}']").first

    def _resolve_new_project_button(self):
        if not self.page:
            return None
        marker = f"flow-worker-new-project-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        try:
            result = self.page.evaluate(
                """(marker) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 24 && r.height >= 24 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],a,div,span"));
                    const rows = [];
                    let best = null;
                    let bestScore = -1e9;
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = metaText(el);
                        if (r.width > 320 || r.height > 180) continue;
                        let score = 0;
                        if (/새\\s*프로젝트|프로젝트\\s*만들|new\\s*project|create\\s*project/.test(meta)) score += 1000;
                        if (/\\+|add|create|만들|생성/.test(meta)) score += 260;
                        if ((el.tagName || "").toLowerCase() === "button") score += 120;
                        if ((el.getAttribute("role") || "").toLowerCase() === "button") score += 90;
                        if (r.x > (window.innerWidth || 1280) * 0.45 && r.y < 180) score += 120;
                        if (/검색|search|정렬|sort|필터|filter|도움|help|설정|setting|삭제|delete|download|다운로드/.test(meta)) score -= 700;
                        if (meta.trim() === "+") score += 420;
                        rows.push({score, meta:meta.slice(0, 180), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a,b) => b.score - a.score);
                    if (!best || bestScore < 250) return {ok:false, rows:rows.slice(0, 10)};
                    document.querySelectorAll("[data-flow-worker-new-project]").forEach((el) => el.removeAttribute("data-flow-worker-new-project"));
                    best.setAttribute("data-flow-worker-new-project", marker);
                    return {ok:true, marker, rows:rows.slice(0, 10)};
                }""",
                marker,
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 새 프로젝트 버튼 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "new-project", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("새 프로젝트 버튼 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-new-project='{marker}']").first

    def _dump_new_project_candidates(self, label: str) -> None:
        if not self.page:
            return
        try:
            rows = self.page.evaluate(
                """() => Array.from(document.querySelectorAll("button,[role='button'],a,div,span"))
                    .map((el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return {
                            vis:r.width >= 12 && r.height >= 12 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0",
                            x:r.x, y:r.y, w:r.width, h:r.height,
                            meta:[el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").slice(0, 220)
                        };
                    })
                    .filter((row) => row.vis && /새|프로젝트|new|project|create|만들|생성|\\+/i.test(row.meta))
                    .slice(0, 60)"""
            ) or []
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 새 프로젝트 후보 덤프 실패: {exc}")
            return
        self._action_log(f"[{self._clock()}] {label}")
        for idx, row in enumerate(rows, start=1):
            self._action_log(
                f"[{self._clock()}]   NEW {idx:02d}. meta='{str(row.get('meta') or '')[:180]}' "
                f"box=({float(row.get('x') or 0.0):.1f},{float(row.get('y') or 0.0):.1f},"
                f"{float(row.get('w') or 0.0):.1f},{float(row.get('h') or 0.0):.1f})"
            )

    def _resolve_prompt_input(self):
        deadline = time.time() + 18.0
        best_rows: list[tuple[float, str, str, dict]] = []
        while time.time() < deadline:
            best = None
            best_selector = ""
            best_score = float("-inf")
            rows: list[tuple[float, str, str, dict]] = []
            for sel in self._input_candidates():
                try:
                    loc = self.page.locator(sel)
                    total = min(loc.count(), 18)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=350):
                            continue
                    except Exception:
                        continue
                    if self._is_asset_search_like_locator(cand):
                        continue
                    score = self._locator_prompt_input_score(cand, sel) - (idx * 6.0)
                    if score == float("-inf"):
                        continue
                    box = self._safe_box(cand)
                    meta = self._locator_meta(cand)
                    if box:
                        rows.append((score, sel, meta[:120], box))
                    if score > best_score:
                        best = cand
                        best_selector = sel
                        best_score = score
            if rows:
                rows.sort(key=lambda row: row[0], reverse=True)
                best_rows = rows[:8]
            if best is not None and best_score > 120.0:
                self._dump_candidate_rows("프롬프트 입력창 후보 상위", best_rows[:5])
                self._action_log(f"[{self._clock()}] 프롬프트 입력창 확정: {best_selector} | score={best_score:.1f}")
                try:
                    best.scroll_into_view_if_needed(timeout=900)
                except Exception:
                    pass
                try:
                    best.click(timeout=1200)
                except Exception:
                    try:
                        best.focus(timeout=1200)
                    except Exception:
                        pass
                return best
            geometric = self._resolve_prompt_input_by_geometry()
            if geometric is not None:
                return geometric
            time.sleep(0.45)
        self._dump_candidate_rows("프롬프트 입력창 후보 최종 실패", best_rows[:8])
        self._dump_dom_input_probe()
        raise RuntimeError("프롬프트 입력창을 찾지 못했습니다.")

    def _input_candidates(self) -> list[str]:
        cands = [
            str(self.cfg.get("input_selector") or "").strip(),
            "textarea[placeholder*='무엇을 만들고 싶으신가요' i]",
            "textarea[aria-label*='무엇을 만들고 싶으신가요' i]",
            "[role='textbox'][aria-label*='무엇을 만들고 싶으신가요' i]",
            "[contenteditable='true'][aria-label*='무엇을 만들고 싶으신가요' i]",
            "#PINHOLE_TEXT_AREA_ELEMENT_ID",
            "textarea#PINHOLE_TEXT_AREA_ELEMENT_ID",
            "[id*='PINHOLE' i]",
            "textarea:not([placeholder*='검색' i]):not([aria-label*='검색' i]):not([placeholder*='asset' i]):not([aria-label*='asset' i])",
            "[role='textbox']:not([aria-label*='검색' i]):not([aria-label*='asset' i])",
            "[contenteditable='true']:not([aria-label*='검색' i]):not([aria-label*='asset' i])",
            "textarea",
            "[contenteditable='true']",
            "[contenteditable='plaintext-only']",
            "div[contenteditable='true']",
            "div[contenteditable='plaintext-only']",
            "[role='textbox']",
            "div.ProseMirror[contenteditable='true']",
            "div[data-lexical-editor='true']",
            "textarea[placeholder*='무엇을 만들' i]",
            "textarea[aria-label*='무엇을 만들' i]",
            "textarea[placeholder*='프롬프트' i]",
            "textarea[aria-label*='프롬프트' i]",
            "textarea[placeholder*='prompt' i]",
            "textarea[placeholder*='message' i]",
            "textarea[placeholder*='메시지' i]",
            "textarea[aria-label*='prompt' i]",
            "textarea[aria-label*='message' i]",
            "[aria-multiline='true']",
            "[data-slate-editor='true']",
            "[spellcheck='true'][contenteditable='true']",
            "[tabindex='0'][contenteditable='true']",
        ]
        return [item for item in dict.fromkeys(item for item in cands if item)]

    def _resolve_prompt_input_by_geometry(self):
        if not self.page:
            return None
        marker = f"flow-worker-prompt-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        try:
            result = self.page.evaluate(
                """(marker) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 20 || r.height < 12) return false;
                        const st = window.getComputedStyle(el);
                        return st && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const attr = (el, name) => (el.getAttribute(name) || "");
                    const metaText = (el) => [
                        el.tagName || "",
                        el.id || "",
                        el.className || "",
                        attr(el, "role"),
                        attr(el, "aria-label"),
                        attr(el, "placeholder"),
                        attr(el, "data-testid"),
                        attr(el, "contenteditable"),
                        attr(el, "aria-multiline"),
                        attr(el, "spellcheck"),
                        el.innerText || "",
                        el.textContent || "",
                    ].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const vw = Math.max(1, window.innerWidth || 1600);
                    const vh = Math.max(1, window.innerHeight || 900);
                    const nodes = Array.from(document.querySelectorAll(
                        "textarea,input:not([type='hidden']),[contenteditable],[role='textbox'],[aria-multiline='true'],[data-slate-editor='true'],[spellcheck='true'],div,form"
                    ));
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!isVisible(el)) continue;
                        const tag = (el.tagName || "").toLowerCase();
                        const role = attr(el, "role").toLowerCase();
                        if (tag === "button" || role === "button") continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 120 || r.height < 28) continue;
                        if (r.y < vh * 0.42 || r.y > vh - 35) continue;
                        const meta = metaText(el);
                        let score = 0;
                        if (meta.includes("무엇을 만들")) score += 2200;
                        if (meta.includes("prompt") || meta.includes("프롬프트") || meta.includes("message") || meta.includes("메시지")) score += 650;
                        if (meta.includes("nano banana") || meta.includes("veo") || meta.includes("x2")) score += 180;
                        if (tag === "textarea") score += 360;
                        if (role === "textbox") score += 260;
                        if (attr(el, "contenteditable").toLowerCase() === "true" || attr(el, "contenteditable").toLowerCase() === "plaintext-only") score += 260;
                        if (attr(el, "aria-multiline").toLowerCase() === "true") score += 220;
                        if (r.width >= 360 && r.width <= Math.min(980, vw * 0.86)) score += 260;
                        else if (r.width > vw * 0.92) score -= 600;
                        if (r.height >= 44 && r.height <= 180) score += 260;
                        else if (r.height > 260) score -= 900;
                        const centerX = r.x + r.width / 2;
                        score -= Math.abs(centerX - vw / 2) * 0.10;
                        score += ((r.y + r.height / 2) / vh) * 260;
                        if (meta.includes("검색") || meta.includes("search") || meta.includes("asset") || meta.includes("에셋") || meta.includes("애셋")) score -= 1800;
                        if (meta.includes("project") || meta.includes("프로젝트") || meta.includes("설정") || meta.includes("settings")) score -= 700;
                        if ((el.innerText || "").length > 900) score -= 900;
                        rows.push({score, tag, x:r.x, y:r.y, w:r.width, h:r.height, meta:meta.slice(0, 140)});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a, b) => b.score - a.score);
                    if (!best || bestScore < 120) return {ok:false, score:bestScore, rows:rows.slice(0, 8)};
                    document.querySelectorAll("[data-flow-worker-prompt-input]").forEach((el) => el.removeAttribute("data-flow-worker-prompt-input"));
                    best.setAttribute("data-flow-worker-prompt-input", marker);
                    const r = best.getBoundingClientRect();
                    return {ok:true, marker, score:bestScore, box:{x:r.x, y:r.y, w:r.width, h:r.height}, rows:rows.slice(0, 8)};
                }""",
                marker,
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 프롬프트 geometry 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), str(row.get("tag") or "dom"), str(row.get("meta") or ""), box))
        self._dump_candidate_rows("프롬프트 geometry 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        locator = self.page.locator(f"[data-flow-worker-prompt-input='{marker}']").first
        box = self._safe_box(locator)
        if not box:
            return None
        click_x = box["x"] + min(max(box["w"] * 0.36, 80.0), max(85.0, box["w"] - 85.0))
        click_y = box["y"] + min(max(box["h"] * 0.52, 22.0), max(24.0, box["h"] - 22.0))
        try:
            self.page.mouse.click(click_x, click_y)
            time.sleep(0.20)
        except Exception:
            try:
                locator.click(timeout=1200)
            except Exception:
                pass
        focused = self._focused_prompt_locator()
        if focused is not None:
            self._action_log(f"[{self._clock()}] 프롬프트 입력창 확정: focused geometry | score={float(result.get('score') or 0.0):.1f}")
            return focused
        self._action_log(f"[{self._clock()}] 프롬프트 입력창 확정: composer geometry | score={float(result.get('score') or 0.0):.1f}")
        return locator

    def _focused_prompt_locator(self):
        try:
            loc = self.page.locator(":focus").first
            if loc.count() <= 0:
                return None
            if not loc.is_visible(timeout=250):
                return None
            box = self._safe_box(loc)
            if not box or box["w"] < 20.0 or box["h"] < 12.0:
                return None
            meta = self._locator_meta(loc)
            if any(k in meta for k in ("검색", "search", "asset", "에셋", "애셋")):
                return None
            return loc
        except Exception:
            return None

    def _dump_dom_input_probe(self) -> None:
        if not self.page:
            return
        try:
            payload = self.page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width > 5 && r.height > 5 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const meta = (el) => [
                        el.tagName || "", el.id || "", el.className || "", el.getAttribute("role") || "",
                        el.getAttribute("aria-label") || "", el.getAttribute("placeholder") || "",
                        el.getAttribute("contenteditable") || "", el.innerText || "", el.textContent || "",
                    ].join(" ").replace(/\\s+/g, " ").slice(0, 180);
                    return {
                        url: location.href,
                        title: document.title,
                        candidates: Array.from(document.querySelectorAll("input,textarea,[contenteditable],[role='textbox'],[aria-multiline],div"))
                            .map((el) => {
                                const r = el.getBoundingClientRect();
                                return {vis:isVisible(el), x:r.x, y:r.y, w:r.width, h:r.height, meta:meta(el)};
                            })
                            .filter((x) => x.vis && (x.y > window.innerHeight * 0.38 || /무엇|prompt|프롬프트|message|메시지|contenteditable/i.test(x.meta)))
                            .slice(0, 20)
                    };
                }"""
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] DOM 입력 후보 덤프 실패: {exc}")
            return
        self._action_log(f"[{self._clock()}] DOM 입력 후보 덤프 URL: {payload.get('url', '')}")
        self._action_log(f"[{self._clock()}] DOM 입력 후보 덤프 TITLE: {payload.get('title', '')}")
        for idx, row in enumerate(list(payload.get("candidates") or []), start=1):
            self._action_log(
                f"[{self._clock()}]   DOM {idx:02d}. meta='{str(row.get('meta') or '')[:160]}' "
                f"box=({float(row.get('x') or 0.0):.1f},{float(row.get('y') or 0.0):.1f},"
                f"{float(row.get('w') or 0.0):.1f},{float(row.get('h') or 0.0):.1f})"
            )

    def _locator_prompt_input_score(self, locator, selector: str = "") -> float:
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
            viewport_h = float((self.page.viewport_size or {}).get("height") or viewport_h)
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
                self._locator_meta(locator),
            ]
        ).lower()
        score = 0.0
        if any(k in meta for k in ("무엇을 만들", "prompt", "프롬프트", "message", "메시지")):
            score += 1700.0
        if any(k in meta for k in ("asset", "search", "에셋", "검색")):
            score -= 2400.0
        if tag == "textarea":
            score += 240.0
        if role == "textbox":
            score += 120.0
        if contenteditable in ("true", "plaintext-only"):
            score += 100.0
        if 180 <= width <= 1200:
            score += 120.0
        elif width > 1500:
            score -= 180.0
        if 28 <= height <= 180:
            score += 220.0
        elif height > 260:
            score -= 1800.0
        if width * height > 260000:
            score -= 1200.0
        y_ratio = (y + height / 2.0) / max(1.0, viewport_h)
        if 0.45 <= y_ratio <= 0.96:
            score += 260.0
        elif y_ratio < 0.22:
            score -= 260.0
        elif y_ratio < 0.34:
            score -= 520.0
        if text_len > 160:
            score -= 700.0
        elif text_len <= 12:
            score += 45.0
        if selector and selector in {"textarea", "[contenteditable='true']", "[contenteditable='plaintext-only']", "div[contenteditable='true']", "div[contenteditable='plaintext-only']", "[role='textbox']"}:
            score -= 120.0
        elif selector:
            score += 90.0
        return score

    def _is_asset_search_like_locator(self, locator) -> bool:
        meta = self._locator_meta(locator)
        has_search = any(k in meta for k in ("asset", "search", "에셋", "검색", "quick-search", "swap_horiz"))
        has_prompt = any(k in meta for k in ("무엇을 만들고 싶으신가요", "prompt", "프롬프트", "message", "메시지"))
        return has_search and not has_prompt

    def _clear_prompt_input(self, input_locator) -> None:
        try:
            input_locator.click(timeout=1200)
        except Exception:
            pass
        try:
            input_locator.press("Control+A", timeout=1200)
            input_locator.press("Backspace", timeout=1200)
            return
        except Exception:
            pass
        try:
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Backspace")
        except Exception:
            pass

    def _type_video_prompt(self, item: PromptBlock, input_locator, log: LogFn) -> None:
        prompt_text = str(item.rendered_prompt or "").strip()
        frame_start = str(item.frame_start_tag or "").strip()
        frame_end = str(item.frame_end_tag or "").strip()
        attached_local = False
        if frame_start:
            local_frame = self._find_local_frame_file(frame_start)
            if local_frame is not None:
                self._attach_local_file_to_prompt(input_locator, local_frame, log, label=f"시작 프레임 {frame_start}")
                attached_local = True
        if frame_start and frame_end:
            log(f"🎞️ 비디오 프레임 라우트 사용: {frame_start}>{frame_end}")
        elif frame_start:
            log(f"🎞️ 비디오 시작 프레임 사용: {frame_start}{' (로컬 첨부)' if attached_local else ''}")
        self._type_prompt_with_inline_references(prompt_text, input_locator, log)

    def _find_local_frame_file(self, frame_tag: str) -> Path | None:
        tag = self._normalize_reference_tag(frame_tag)
        if not tag:
            return None
        output_dir = Path(str(self.cfg.get("download_output_dir") or "").strip() or str((self.base_dir / "downloads").resolve()))
        candidates = []
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            candidates.append(output_dir / f"{tag}{ext}")
            candidates.append(output_dir / f"@{tag}{ext}")
        for path in candidates:
            if path.exists() and path.is_file():
                return path
        return None

    def _attach_local_file_to_prompt(self, input_locator, file_path: Path, log: LogFn, *, label: str = "로컬 파일") -> None:
        path = Path(file_path).resolve()
        if not path.exists():
            raise RuntimeError(f"{label} 파일이 없습니다: {path}")
        log(f"📎 {label} 첨부 시작: {path.name}")
        self._action_log(f"[{self._clock()}] {label} 첨부 시작: {path}")
        before_signature = self._composer_attachment_signature(input_locator)
        errors: list[str] = []

        button = self._resolve_prompt_local_upload_button(input_locator)
        if button is not None:
            with self.page.expect_file_chooser(timeout=7000) as file_chooser_info:
                if not self._click_with_actor_fallback(button, f"{label} 첨부 버튼"):
                    button.click(timeout=2500, force=True)
            file_chooser_info.value.set_files(str(path))
            self._action_log(f"[{self._clock()}] {label} file chooser 첨부")
            if self._wait_for_composer_attachment(input_locator, before_signature, path.name):
                log(f"✅ {label} 첨부 완료: {path.name}")
                return
            errors.append("file chooser 후 composer 첨부 검증 실패")
        else:
            errors.append("첨부 버튼 미탐지")

        file_input = self._resolve_upload_file_input()
        if file_input is not None:
            try:
                file_input.set_input_files(str(path), timeout=5000)
                self._action_log(f"[{self._clock()}] {label} file input 직접 첨부")
                if self._wait_for_composer_attachment(input_locator, before_signature, path.name):
                    log(f"✅ {label} 첨부 완료: {path.name}")
                    return
                errors.append("file input 후 composer 첨부 검증 실패")
            except Exception as exc:
                errors.append(f"file input 첨부 실패: {exc}")

        self._dump_local_upload_candidates(f"{label} 첨부 검증 실패")
        reason = " | ".join(errors) if errors else "알 수 없는 첨부 실패"
        raise RuntimeError(f"{label}를 Flow 입력창에 첨부하지 못했습니다: {reason}")

    def _attach_config_reference_files(self, input_locator, log: LogFn) -> None:
        raw_files = self.cfg.get("flow_reference_files") or []
        if isinstance(raw_files, str):
            raw_files = [part.strip() for part in re.split(r"[;\n]", raw_files) if part.strip()]
        if not isinstance(raw_files, list) or not raw_files:
            return
        for idx, raw in enumerate(raw_files, start=1):
            text = str(raw or "").strip()
            if not text:
                continue
            path = Path(text)
            if not path.is_absolute():
                path = self.base_dir / path
            self._attach_local_file_to_prompt(input_locator, path, log, label=f"레퍼런스 {idx}")

    def _composer_attachment_signature(self, input_locator) -> dict[str, object]:
        if not self.page:
            return {"count": 0, "meta": ""}
        input_box = self._safe_box(input_locator) if input_locator is not None else None
        try:
            return self.page.evaluate(
                """(inputBox) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 18 && r.height >= 18 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const inRegion = (el) => {
                        const r = el.getBoundingClientRect();
                        const cx = r.x + r.width / 2;
                        const cy = r.y + r.height / 2;
                        if (!inputBox) return cy > (window.innerHeight || 900) * 0.52;
                        const left = inputBox.x - 160;
                        const right = inputBox.x + inputBox.w + 260;
                        const top = inputBox.y - 260;
                        const bottom = inputBox.y + inputBox.h + 170;
                        return cx >= left && cx <= right && cy >= top && cy <= bottom;
                    };
                    const nodes = Array.from(document.querySelectorAll("img,video,canvas,[role='img'],button,[role='button'],div,span"));
                    const rows = [];
                    for (const el of nodes) {
                        if (!visible(el) || !inRegion(el)) continue;
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        const bg = st.backgroundImage || "";
                        const meta = [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||"", bg].join(" ").replace(/\\s+/g, " ");
                        const hasMedia = /IMG|VIDEO|CANVAS/i.test(el.tagName || "") || /url\\(/i.test(bg) || /image|이미지|frame|프레임|시작|종료|asset|에셋/i.test(meta);
                        if (!hasMedia) continue;
                        if (r.width > 460 || r.height > 360) continue;
                        rows.push({meta:meta.slice(0, 180), x:r.x, y:r.y, w:r.width, h:r.height});
                    }
                    return {count: rows.length, meta: rows.map((row) => row.meta).join(" | ").slice(0, 1000)};
                }""",
                input_box,
            ) or {"count": 0, "meta": ""}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] composer 첨부 시그니처 실패: {exc}")
            return {"count": 0, "meta": ""}

    def _wait_for_composer_attachment(self, input_locator, before_signature: dict[str, object], file_name: str) -> bool:
        before_count = int((before_signature or {}).get("count") or 0)
        file_stem = Path(str(file_name or "")).stem.lower()
        deadline = time.time() + 7.0
        while time.time() < deadline:
            time.sleep(0.4)
            after = self._composer_attachment_signature(input_locator)
            after_count = int((after or {}).get("count") or 0)
            after_meta = str((after or {}).get("meta") or "").lower()
            if after_count > before_count:
                self._action_log(f"[{self._clock()}] composer 첨부 검증 성공: count {before_count}->{after_count}")
                return True
            if file_stem and file_stem in after_meta:
                self._action_log(f"[{self._clock()}] composer 첨부 검증 성공: filename {file_stem}")
                return True
        after = self._composer_attachment_signature(input_locator)
        self._action_log(
            f"[{self._clock()}] composer 첨부 검증 실패: count {before_count}->{int((after or {}).get('count') or 0)} "
            f"meta='{str((after or {}).get('meta') or '')[:700]}'"
        )
        return False

    def _resolve_upload_file_input(self):
        if not self.page:
            return None
        try:
            loc = self.page.locator("input[type='file']")
            total = min(loc.count(), 20)
        except Exception:
            return None
        best = None
        best_score = float("-inf")
        for idx in range(total):
            cand = loc.nth(idx)
            meta = self._locator_meta(cand)
            score = 0.0
            if any(token in meta for token in ("image", "이미지", "asset", "upload", "업로드", "file")):
                score += 200.0
            if any(token in meta for token in ("video", "영상", "동영상")):
                score += 70.0
            try:
                accepts = str(cand.get_attribute("accept") or "").lower()
                if "image" in accepts:
                    score += 300.0
                if "video" in accepts:
                    score += 40.0
            except Exception:
                pass
            if score > best_score:
                best = cand
                best_score = score
        return best if best is not None and best_score >= 0.0 else None

    def _resolve_prompt_local_upload_button(self, input_locator):
        if not self.page:
            return None
        marker = f"flow-worker-local-upload-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        input_box = self._safe_box(input_locator) if input_locator is not None else None
        try:
            result = self.page.evaluate(
                """(payload) => {
                    const marker = payload.marker;
                    const inputBox = payload.inputBox || null;
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 22 && r.height >= 22 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],div,span"));
                    const vw = window.innerWidth || 1280;
                    const vh = window.innerHeight || 900;
                    const targetX = inputBox ? inputBox.x + 28 : vw * 0.18;
                    const targetY = inputBox ? inputBox.y + inputBox.h * 0.5 : vh * 0.84;
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = metaText(el);
                        const cx = r.x + r.width / 2;
                        const cy = r.y + r.height / 2;
                        if (inputBox) {
                            if (cy < inputBox.y - 45 || cy > inputBox.y + inputBox.h + 70) continue;
                            if (cx < inputBox.x - 45 || cx > inputBox.x + Math.max(130, inputBox.w * 0.28)) continue;
                        } else if (cy < vh * 0.55) {
                            continue;
                        }
                        if (r.width > 120 || r.height > 120) continue;
                        let score = 500;
                        if (/\\+|add|추가|upload|업로드|attach|첨부|image|이미지/.test(meta)) score += 260;
                        if (meta.trim() === "+" || meta.includes("add")) score += 220;
                        if ((el.tagName || "").toLowerCase() === "button") score += 100;
                        if ((el.getAttribute("role") || "").toLowerCase() === "button") score += 80;
                        if (/생성|generate|send|보내|검색|search|download|다운로드|설정|settings/.test(meta)) score -= 520;
                        score -= Math.abs(cx - targetX) * 1.1;
                        score -= Math.abs(cy - targetY) * 1.0;
                        rows.push({score, meta:meta.slice(0, 140), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a,b) => b.score - a.score);
                    if (!best || bestScore < 180) return {ok:false, rows:rows.slice(0, 10)};
                    document.querySelectorAll("[data-flow-worker-local-upload]").forEach((el) => el.removeAttribute("data-flow-worker-local-upload"));
                    best.setAttribute("data-flow-worker-local-upload", marker);
                    return {ok:true, marker, rows:rows.slice(0, 10)};
                }""",
                {"marker": marker, "inputBox": input_box},
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 로컬 첨부 버튼 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "local-upload", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("로컬 첨부 버튼 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-local-upload='{marker}']").first

    def _dump_local_upload_candidates(self, label: str) -> None:
        if not self.page:
            return
        try:
            rows = self.page.evaluate(
                """() => Array.from(document.querySelectorAll("button,[role='button'],input[type='file'],div,span"))
                    .map((el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return {
                            vis:r.width >= 8 && r.height >= 8 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0",
                            x:r.x, y:r.y, w:r.width, h:r.height,
                            meta:[el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.getAttribute("accept")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").slice(0, 180)
                        };
                    })
                    .filter((row) => row.vis && /\\+|add|upload|업로드|attach|첨부|image|이미지|asset|에셋|file/i.test(row.meta))
                    .slice(0, 50)"""
            ) or []
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 로컬 첨부 후보 덤프 실패: {exc}")
            return
        self._action_log(f"[{self._clock()}] {label}")
        for idx, row in enumerate(rows, start=1):
            self._action_log(
                f"[{self._clock()}]   UPLOAD {idx:02d}. meta='{str(row.get('meta') or '')[:160]}' "
                f"box=({float(row.get('x') or 0.0):.1f},{float(row.get('y') or 0.0):.1f},"
                f"{float(row.get('w') or 0.0):.1f},{float(row.get('h') or 0.0):.1f})"
            )

    def _split_prompt_inline_reference_parts(self, prompt_text: str) -> list[dict[str, str]]:
        text = str(prompt_text or "")
        pattern = re.compile(r"@(S?\d{3,4})(?!\d)", re.IGNORECASE)
        parts: list[dict[str, str]] = []
        cursor = 0
        for match in pattern.finditer(text):
            if match.start() > cursor:
                parts.append({"type": "text", "value": text[cursor:match.start()]})
            token = self._normalize_reference_tag(match.group(1))
            parts.append({"type": "reference", "value": token})
            cursor = match.end()
        if cursor < len(text):
            parts.append({"type": "text", "value": text[cursor:]})
        return parts

    def _type_prompt_with_inline_references(self, prompt_text: str, input_locator, log: LogFn) -> None:
        parts = self._split_prompt_inline_reference_parts(prompt_text)
        self.has_inline_prompt_refs = any(part.get("type") == "reference" for part in parts)
        if not self.has_inline_prompt_refs:
            self.actor.type_text(prompt_text, input_locator=input_locator, mode="typing")
            return
        log(f"🔖 프롬프트 inline 레퍼런스 감지: {sum(1 for part in parts if part.get('type') == 'reference')}개")
        keep_focus_only = False
        for idx, part in enumerate(parts):
            if part.get("type") == "text":
                chunk = str(part.get("value") or "")
                if chunk:
                    if keep_focus_only:
                        next_has_reference = any(later.get("type") == "reference" for later in parts[idx + 1 :])
                        if next_has_reference:
                            self._type_prompt_inline_text_chunk(chunk, input_locator)
                        else:
                            protected_len = min(len(chunk), 18)
                            protected_chunk = chunk[:protected_len]
                            remaining_chunk = chunk[protected_len:]
                            if protected_chunk:
                                self._type_prompt_inline_text_chunk(protected_chunk, input_locator)
                            if remaining_chunk:
                                self.actor.type_text(remaining_chunk, input_locator=None, mode="typing")
                    else:
                        self.actor.type_text(chunk, input_locator=input_locator, mode="typing")
                    keep_focus_only = True
                continue
            asset_tag = str(part.get("value") or "").strip()
            if asset_tag:
                input_locator = self._attach_reference(input_locator, asset_tag, log)
                keep_focus_only = True

    def _type_prompt_inline_text_chunk(self, text: str, input_locator) -> None:
        if not text:
            return
        self._action_log(f"[{self._clock()}] inline 텍스트 직선 입력 시작 | len={len(text)}")
        fatigue = self.actor.get_fatigue_factor()
        typing_speed = getattr(self.actor, "typing_speed_factor", 1.0)
        for ch in text:
            try:
                if ch == "\n":
                    self.page.keyboard.press("Shift+Enter")
                else:
                    self.page.keyboard.type(ch)
            except Exception:
                try:
                    self.page.keyboard.insert_text(ch)
                except Exception:
                    try:
                        input_locator.type(ch, delay=25)
                    except Exception:
                        pass
            if ch in (" ", "\n"):
                base_min, base_max = 0.015, 0.06
            elif ch in ".,!?:;)(":
                base_min, base_max = 0.02, 0.09
            else:
                base_min, base_max = 0.025, 0.11
            speed = max(0.45, min(typing_speed * random.uniform(0.7, 1.3), 8.0))
            fatigue_slow = 1.0 + max(0.0, 1.0 - fatigue) * 0.45
            time.sleep(max(0.004, min(random.uniform(base_min, base_max) * (1.0 / speed) * fatigue_slow, 0.18)))
        self._action_log(f"[{self._clock()}] inline 텍스트 직선 입력 완료")

    def _attach_reference(self, input_locator, asset_tag: str, log: LogFn) -> None:
        log(f"🔖 레퍼런스 첨부 시작: {asset_tag}")
        self._action_log(f"[{self._clock()}] 레퍼런스 첨부 시작: {asset_tag}")
        search_input = self._open_prompt_reference_search_via_keyboard(input_locator, timeout_sec=2.4)
        search_input = self._fill_prompt_reference_search_input(search_input, asset_tag)
        self.actor.random_action_delay("레퍼런스 Enter 전 대기", 0.04, 0.10)
        self.page.keyboard.press("Enter")
        self._action_log(f"[{self._clock()}] 레퍼런스 Enter 선택: {asset_tag}")
        self.actor.random_action_delay("레퍼런스 Enter 반영 대기", 0.08, 0.18)
        log(f"✅ 레퍼런스 첨부 요청 완료: {asset_tag}")
        self.actor.random_action_delay("레퍼런스 첨부 반영 대기", 0.04, 0.10)
        log("🧭 레퍼런스 첨부 후 입력창 복귀: Enter만 사용")
        return input_locator

    def _open_prompt_reference_search_via_keyboard(self, input_locator, timeout_sec: float = 2.2):
        if not self.page or input_locator is None:
            raise RuntimeError("프롬프트 입력창이 없어 @ 레퍼런스 호출을 할 수 없습니다.")
        deadline = time.time() + max(1.0, timeout_sec)
        last_error = "search-input-not-found"
        trigger_methods = ("page_type_at", "locator_type_at", "page_shift2", "locator_shift2", "js_dispatch")
        while time.time() < deadline:
            for method in trigger_methods:
                before_text = self._read_input_text(input_locator)
                try:
                    input_locator.focus(timeout=900)
                except Exception:
                    pass
                try:
                    if method == "page_type_at":
                        self.page.keyboard.type("@")
                        self._action_log(f"[{self._clock()}] 레퍼런스 @ 트리거 입력: page type('@')")
                    elif method == "locator_type_at":
                        input_locator.type("@", delay=random.randint(30, 80), timeout=1200)
                        self._action_log(f"[{self._clock()}] 레퍼런스 @ 트리거 입력: locator type('@')")
                    elif method == "page_shift2":
                        self.page.keyboard.down("Shift")
                        self.page.keyboard.press("2")
                        self.page.keyboard.up("Shift")
                        self._action_log(f"[{self._clock()}] 레퍼런스 @ 트리거 입력: page Shift+2")
                    elif method == "locator_shift2":
                        input_locator.press("Shift+2", timeout=1200)
                        self._action_log(f"[{self._clock()}] 레퍼런스 @ 트리거 입력: locator Shift+2")
                    else:
                        self.page.evaluate(
                            """() => {
                                const el = document.activeElement;
                                if (!el) return false;
                                if ("value" in el) {
                                    const start = el.selectionStart ?? String(el.value || "").length;
                                    const end = el.selectionEnd ?? start;
                                    el.value = String(el.value || "").slice(0, start) + "@" + String(el.value || "").slice(end);
                                    if (el.setSelectionRange) el.setSelectionRange(start + 1, start + 1);
                                } else if (el.isContentEditable) {
                                    document.execCommand("insertText", false, "@");
                                }
                                el.dispatchEvent(new InputEvent("input", {bubbles:true, data:"@", inputType:"insertText"}));
                                return true;
                            }"""
                        )
                        self._action_log(f"[{self._clock()}] 레퍼런스 @ 트리거 입력: js dispatch")
                except Exception as exc:
                    last_error = str(exc)
                self.actor.random_action_delay("레퍼런스 검색창 표시 대기", 0.25, 0.7)
                after_text = self._read_input_text(input_locator)
                typed_at = after_text.endswith("@") or (after_text.count("@") > before_text.count("@"))
                search_input = self._resolve_overlay_search_input(timeout_sec=0.9)
                if search_input is not None and typed_at:
                    self._action_log(f"[{self._clock()}] 레퍼런스 @ 호출 성공: {method}")
                    return search_input
                try:
                    input_locator.focus(timeout=800)
                    current_text = self._read_input_text(input_locator)
                    extra_count = max(0, len(current_text) - len(before_text))
                    if extra_count > 0 and current_text.startswith(before_text):
                        for _ in range(extra_count):
                            self.page.keyboard.press("Backspace")
                    elif current_text.endswith("@"):
                        self.page.keyboard.press("Backspace")
                except Exception:
                    pass
                time.sleep(0.10)
        raise RuntimeError(f"@ 레퍼런스 검색창 호출 실패 ({last_error})")

    def _fill_prompt_reference_search_input(self, search_input, asset_tag: str):
        expected = self._normalize_reference_tag(asset_tag)

        def _try_fill(loc):
            if loc is None:
                return None
            box = self._safe_box(loc)
            meta = self._locator_meta(loc)
            meta_has_search = any(k in meta for k in ("검색", "search", "asset", "애셋", "에셋", "quick-search"))
            if box and not self._is_prompt_reference_overlay_input_box(box) and not meta_has_search:
                return None
            try:
                loc.click(timeout=350)
            except Exception:
                try:
                    loc.focus(timeout=300)
                except Exception:
                    return None
            try:
                self.page.keyboard.press("Control+A")
                self.page.keyboard.press("Backspace")
            except Exception:
                pass
            try:
                loc.fill(asset_tag, timeout=500)
            except Exception:
                try:
                    loc.type(asset_tag, delay=random.randint(25, 70), timeout=500)
                except Exception:
                    return None
            time.sleep(0.06)
            typed = self._normalize_reference_tag(self._read_input_text(loc))
            if typed == expected:
                return loc
            return None

        filled = _try_fill(search_input)
        if filled is None:
            filled = _try_fill(self._resolve_overlay_search_input(timeout_sec=0.9))
        if filled is None:
            ok, reason = self._direct_fill_reference_search_via_dom(asset_tag)
            if not ok:
                raise RuntimeError(f"레퍼런스 검색창을 찾지 못했습니다. ({reason})")
            self._action_log(f"[{self._clock()}] 레퍼런스 검색 입력: {asset_tag} (DOM 직접입력)")
            return None
        self._action_log(f"[{self._clock()}] 레퍼런스 검색 입력: {asset_tag} (자동 탐색)")
        return filled

    def _resolve_overlay_search_input(self, timeout_sec: float = 2.0):
        selectors = [
            str(self.cfg.get("prompt_reference_search_input_selector") or "").strip(),
            "input",
            "[role='searchbox']",
            "[role='textbox']",
            "textarea",
            "[contenteditable='true']",
        ]
        selectors = [item for item in dict.fromkeys(selectors) if item and item != "자동 탐색"]
        deadline = time.time() + max(0.5, timeout_sec)
        best_rows = []
        while time.time() < deadline:
            best = None
            best_score = float("-inf")
            rows = []
            for sel in selectors:
                try:
                    loc = self.page.locator(sel)
                    total = min(loc.count(), 40)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=250):
                            continue
                    except Exception:
                        continue
                    box = self._safe_box(cand)
                    if not box:
                        continue
                    meta = self._locator_meta(cand)
                    width = float(box["w"])
                    height = float(box["h"])
                    x = float(box["x"])
                    y = float(box["y"])
                    meta_has_search = any(k in meta for k in ("검색", "search", "asset", "에셋", "recent", "최근", "quick-search"))
                    overlay_shape = self._is_prompt_reference_overlay_input_box(box)
                    if not overlay_shape and not meta_has_search:
                        continue
                    score = 0.0
                    if y <= 180.0:
                        score += 720.0
                    elif y <= 260.0:
                        score += 260.0
                    else:
                        score -= 1600.0
                    if 220.0 <= width <= 980.0:
                        score += 220.0
                    elif width > 1200.0:
                        score -= 400.0
                    score -= abs((x + width / 2.0) - 420.0) * 0.22
                    if meta_has_search:
                        score += 520.0
                    if overlay_shape:
                        score += 260.0
                    if any(k in meta for k in ("무엇을 만들", "prompt", "프롬프트", "message", "메시지", "nano banana", "veo", "동영상", "이미지")):
                        score -= 1400.0
                    rows.append((score, sel, meta[:120], box))
                    if score > best_score:
                        best = cand
                        best_score = score
            if rows:
                rows.sort(key=lambda row: row[0], reverse=True)
                best_rows = rows[:6]
            if best is not None and best_score > 150.0:
                self._dump_candidate_rows("레퍼런스 검색창 후보 상위", best_rows[:4])
                return best
            time.sleep(0.12)
        self._dump_candidate_rows("레퍼런스 검색창 후보 실패", best_rows[:4])
        return None

    def _submit_prompt(self, input_locator) -> None:
        before_text = self._read_input_text(input_locator)
        submit = self._resolve_submit_button(input_locator)
        submit_before_state = self._capture_submit_state(submit)
        indicator_before = self._is_generation_indicator_visible()
        notes: list[str] = []
        submitted = False
        reason = ""
        self._action_log(f"[{self._clock()}] 제출 정책: 안전 단일 제출")
        try:
            input_locator.click(timeout=1200)
        except Exception:
            pass
        if self.has_inline_prompt_refs and submit is not None:
            self._action_log(f"[{self._clock()}] 제출 시도: 버튼 클릭 우선")
            clicked = self._click_with_actor_fallback(submit, "제출 버튼")
            if clicked:
                submitted, reason = self._confirm_submission_started(
                    input_locator,
                    before_text,
                    timeout_sec=5,
                    submit_locator=submit,
                    submit_before_state=submit_before_state,
                    indicator_before=indicator_before,
                )
            else:
                reason = "button_click_failed"
            notes.append(f"Button={'OK' if submitted else 'FAIL'}({reason})")
        else:
            self.actor.random_action_delay("Enter 제출 전 딜레이", 0.2, 0.8)
            self.page.keyboard.press("Enter")
            self._action_log(f"[{self._clock()}] 제출 시도: Enter(단일 1회)")
            submitted, reason = self._confirm_submission_started(
                input_locator,
                before_text,
                timeout_sec=4,
                submit_locator=submit,
                submit_before_state=submit_before_state,
                indicator_before=indicator_before,
            )
            notes.append(f"Enter={'OK' if submitted else 'FAIL'}({reason})")
        if not submitted and submit is not None:
            self._action_log(f"[{self._clock()}] 제출 폴백 시도: 버튼 클릭")
            clicked = self._click_with_actor_fallback(submit, "제출 버튼")
            if clicked:
                submitted, reason = self._confirm_submission_started(
                    input_locator,
                    before_text,
                    timeout_sec=8,
                    submit_locator=submit,
                    submit_before_state=submit_before_state,
                    indicator_before=indicator_before,
                )
            else:
                reason = "button_click_failed"
            notes.append(f"Button={'OK' if submitted else 'FAIL'}({reason})")
        if not submitted:
            raise RuntimeError(f"제출 확인 실패(생성 시작 신호 없음): {', '.join(notes)}")
        self._action_log(f"[{self._clock()}] 제출 검증 완료: {', '.join(notes)}")

    def _resolve_submit_button(self, input_locator):
        try:
            input_box = input_locator.bounding_box()
        except Exception:
            input_box = None
        submit = self._resolve_submit_by_geometry(input_locator)
        if submit is not None:
            return submit
        selectors = [
            str(self.cfg.get("submit_selector") or "").strip(),
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
        ]
        selectors = [item for item in dict.fromkeys(selectors) if item]
        best = None
        best_score = float("-inf")
        for sel in selectors:
            try:
                loc = self.page.locator(sel)
                total = min(loc.count(), 100)
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
                if not box or float(box["width"]) < 20.0 or float(box["height"]) < 20.0:
                    continue
                if self._reject_submit_candidate(cand, input_box):
                    continue
                meta = self._locator_meta(cand)
                score = 0.0
                if any(token in meta for token in ("생성", "create", "generate", "submit")):
                    score += 520.0
                if input_box:
                    input_cy = float(input_box["y"]) + float(input_box["height"]) * 0.5
                    input_right = float(input_box["x"]) + float(input_box["width"])
                    cx = float(box["x"]) + float(box["width"]) * 0.5
                    cy = float(box["y"]) + float(box["height"]) * 0.5
                    score -= abs(cy - input_cy) * 1.2
                    score -= abs(cx - (input_right + 36.0)) * 0.4
                    if cx >= input_right - 30.0:
                        score += 180.0
                else:
                    score += float(box["y"]) * 0.2
                if float(box["y"]) < 500.0:
                    score -= 220.0
                if score > best_score:
                    best = cand
                    best_score = score
        return best

    def _resolve_submit_by_geometry(self, input_locator):
        input_box = self._safe_box(input_locator)
        if not input_box:
            return None
        ix = input_box["x"]
        iy = input_box["y"]
        iw = input_box["w"]
        ih = input_box["h"]
        input_cy = iy + ih / 2.0
        input_right = ix + iw
        best = None
        best_score = float("inf")
        try:
            loc = self.page.locator("button, [role='button']")
            total = min(loc.count(), 250)
        except Exception:
            return None
        for idx in range(total):
            cand = loc.nth(idx)
            try:
                if not cand.is_visible(timeout=300):
                    continue
            except Exception:
                continue
            box = self._safe_box(cand)
            if not box:
                continue
            if self._reject_submit_candidate(cand, {"x": ix, "y": iy, "width": iw, "height": ih}):
                continue
            cx = box["x"] + box["w"] / 2.0
            cy = box["y"] + box["h"] / 2.0
            score = abs(cy - input_cy) * 6.0
            score += abs(cx - (input_right + 26.0)) * 2.8
            score += abs(box["w"] - 46.0) * 1.2
            score += abs(box["h"] - 46.0) * 1.2
            meta = self._locator_meta(cand)
            if any(x in meta for x in ("생성", "generate", "send", "보내", "arrow", "forward", "submit")):
                score -= 260.0
            if score < best_score:
                best = cand
                best_score = score
        return best

    def _reject_submit_candidate(self, locator, input_box) -> bool:
        if input_box is None:
            return False
        box = self._safe_box(locator)
        if not box:
            return True
        ix = float(input_box.get("x") or 0.0)
        iy = float(input_box.get("y") or 0.0)
        iw = float(input_box.get("width", input_box.get("w", 0.0)) or 0.0)
        ih = float(input_box.get("height", input_box.get("h", 0.0)) or 0.0)
        cx = box["x"] + box["w"] / 2.0
        cy = box["y"] + box["h"] / 2.0
        if box["w"] < 20.0 or box["h"] < 20.0:
            return True
        if box["w"] > 84.0 or box["h"] > 84.0:
            return True
        if cy < (iy - 36.0) or cy > (iy + ih + 36.0):
            return True
        if cx < (ix + iw * 0.82):
            return True
        if cx > (ix + iw + 110.0):
            return True
        meta = self._locator_meta(locator)
        noise = ("애셋", "에셋", "asset", "검색", "search", "오래된", "정렬", "업로드", "upload", "이미지", "영상", "video", "image", "nano", "banana", "crop", "x2", "x3", "x4", "모델", "model", "menu", "메뉴", "설정", "setting", "settings", "도움", "help", "프로젝트")
        return any(token in meta for token in noise)

    def _switch_media_mode(self, media_mode: str, log: LogFn) -> None:
        desired = "video" if str(media_mode or "").strip().lower() == "video" else "image"
        input_locator = None
        try:
            input_locator = self._resolve_prompt_input()
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 생성 옵션 기준 입력창 탐지 실패: {exc}")

        before = self._detect_generation_media_state(input_locator=input_locator)
        desired_variant = self._desired_variant_count(desired)
        log(f"🎛️ 생성 옵션 확인 시작: 목표={desired} | 현재={before or '미확인'} | 개수={desired_variant}")
        self._action_log(f"[{self._clock()}] 생성 옵션 확인 시작: 목표={desired} | 현재={before or '미확인'} | 개수={desired_variant}")

        if before == desired and self._generation_variant_matches(desired_variant, input_locator=input_locator):
            log(f"🎛️ 생성 옵션 유지: {desired} {desired_variant}")
            self._action_log(f"[{self._clock()}] 생성 옵션 유지: {desired} {desired_variant}")
            return

        opener = self._resolve_generation_options_button(input_locator=input_locator)
        if opener is None:
            raise RuntimeError("생성 옵션 패널 버튼을 찾지 못했습니다.")
        if not self._click_with_actor_fallback(opener, "생성 옵션 패널"):
            raise RuntimeError("생성 옵션 패널 열기 실패")
        time.sleep(0.45)

        if before != desired:
            media_button = self._resolve_generation_panel_choice("media", desired, input_locator=input_locator)
            if media_button is None:
                self._dump_generation_panel_candidates("media", desired, input_locator=input_locator)
                raise RuntimeError(f"생성 옵션 패널에서 {desired} 버튼을 찾지 못했습니다.")
            if not self._click_with_actor_fallback(media_button, f"{desired} 선택"):
                raise RuntimeError(f"{desired} 선택 클릭 실패")
            log(f"🎛️ 생성 모드 선택: {desired}")
            self._action_log(f"[{self._clock()}] 생성 모드 선택: {desired}")
            time.sleep(0.55)

        variant_button = self._resolve_generation_panel_choice("variant", desired_variant, input_locator=input_locator)
        if variant_button is None:
            self._dump_generation_panel_candidates("variant", desired_variant, input_locator=input_locator)
            raise RuntimeError(f"생성 옵션 패널에서 {desired_variant} 버튼을 찾지 못했습니다.")
        if not self._click_with_actor_fallback(variant_button, f"{desired_variant} 선택"):
            raise RuntimeError(f"{desired_variant} 선택 클릭 실패")
        log(f"🎛️ 생성 개수 선택: {desired_variant}")
        self._action_log(f"[{self._clock()}] 생성 개수 선택: {desired_variant}")
        time.sleep(0.35)

        try:
            self.page.keyboard.press("Escape")
            time.sleep(0.25)
        except Exception:
            pass

        verified = self._detect_generation_media_state(input_locator=input_locator)
        if verified != desired:
            raise RuntimeError(f"생성 모드 전환 검증 실패: 기대={desired} / 감지={verified or '미확인'}")
        if not self._generation_variant_matches(desired_variant, input_locator=input_locator):
            raise RuntimeError(f"생성 개수 전환 검증 실패: 기대={desired_variant}")
        log(f"🎛️ 생성 옵션 확정: {desired} {desired_variant}")
        self._action_log(f"[{self._clock()}] 생성 옵션 확정: {desired} {desired_variant}")

    def _desired_variant_count(self, media_mode: str) -> str:
        key = "video_variant_count" if media_mode == "video" else "image_variant_count"
        raw = str(self.cfg.get(key) or "x1").strip().lower()
        if raw in {"1", "2", "3", "4"}:
            raw = f"x{raw}"
        if raw not in {"x1", "x2", "x3", "x4"}:
            raw = "x1"
        return raw

    def _detect_generation_media_state(self, input_locator=None) -> str | None:
        opener = self._resolve_generation_options_button(input_locator=input_locator)
        if opener is None:
            return None
        meta = self._locator_meta(opener)
        if any(token in meta for token in ("동영상", "영상", "video", "veo")):
            return "video"
        if any(token in meta for token in ("이미지", "image", "nano", "banana")):
            return "image"
        return None

    def _generation_variant_matches(self, variant: str, input_locator=None) -> bool:
        opener = self._resolve_generation_options_button(input_locator=input_locator)
        if opener is None:
            return False
        meta = self._locator_meta(opener).replace(" ", "").lower()
        return any(alias in meta for alias in self._variant_aliases(variant))

    @staticmethod
    def _variant_aliases(variant: str) -> list[str]:
        raw = str(variant or "").replace(" ", "").lower()
        match = re.search(r"([1-4])", raw)
        if not match:
            return [raw] if raw else []
        n = match.group(1)
        return [f"x{n}", f"{n}x"]

    def _resolve_generation_options_button(self, input_locator=None):
        if not self.page:
            return None
        marker = f"flow-worker-options-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        input_box = self._safe_box(input_locator) if input_locator is not None else None
        try:
            result = self.page.evaluate(
                """(payload) => {
                    const marker = payload.marker;
                    const inputBox = payload.inputBox || null;
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 60 && r.height >= 22 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [
                        el.tagName || "",
                        el.getAttribute("role") || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("title") || "",
                        el.innerText || "",
                        el.textContent || "",
                    ].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],[role='tab'],div,span"));
                    const vw = Math.max(1, window.innerWidth || 1200);
                    const vh = Math.max(1, window.innerHeight || 900);
                    let targetX = vw * 0.70;
                    let targetY = vh * 0.78;
                    if (inputBox) {
                        targetX = inputBox.x + inputBox.w * 0.78;
                        targetY = inputBox.y + inputBox.h * 0.5;
                    }
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = metaText(el);
                        const hasMode = /nano|banana|이미지|image|동영상|영상|video|veo/.test(meta);
                        const hasCount = /\\b(?:x[1-4]|[1-4]x)\\b/.test(meta);
                        if (!hasMode && !hasCount) continue;
                        if (/생성\\s*$|generate|arrow_forward|검색|search|필터|정렬|dashboard|장면|scene|도움|help|설정|settings/.test(meta)) continue;
                        if (r.width > 330 || r.height > 90) continue;
                        if (r.x < 100) continue;
                        const cx = r.x + r.width / 2;
                        const cy = r.y + r.height / 2;
                        let score = 800;
                        score -= Math.abs(cx - targetX) * 0.65;
                        score -= Math.abs(cy - targetY) * 2.4;
                        if (inputBox) {
                            if (cy < inputBox.y - 30 || cy > inputBox.y + inputBox.h + 70) score -= 900;
                            if (cx < inputBox.x + inputBox.w * 0.48) score -= 450;
                        } else {
                            if (cy < vh * 0.45 || cy > vh - 20) score -= 500;
                        }
                        if (hasMode) score += 220;
                        if (hasCount) score += 180;
                        rows.push({score, meta:meta.slice(0, 140), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a, b) => b.score - a.score);
                    if (!best || bestScore < 120) return {ok:false, rows:rows.slice(0, 8)};
                    document.querySelectorAll("[data-flow-worker-options-button]").forEach((el) => el.removeAttribute("data-flow-worker-options-button"));
                    best.setAttribute("data-flow-worker-options-button", marker);
                    return {ok:true, marker, rows:rows.slice(0, 8)};
                }""",
                {"marker": marker, "inputBox": input_box},
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 생성 옵션 버튼 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "options", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("생성 옵션 버튼 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-options-button='{marker}']").first

    def _resolve_generation_panel_choice(self, choice_type: str, value: str, input_locator=None):
        if not self.page:
            return None
        marker = f"flow-worker-panel-choice-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        input_box = self._safe_box(input_locator) if input_locator is not None else None
        choice_type = "variant" if choice_type == "variant" else "media"
        value = str(value or "").strip().lower()
        try:
            result = self.page.evaluate(
                """(payload) => {
                    const marker = payload.marker;
                    const kind = payload.kind;
                    const value = String(payload.value || "").toLowerCase();
                    const inputBox = payload.inputBox || null;
                    const targetTexts = kind === "media"
                        ? (value === "video" ? ["동영상", "video", "영상"] : ["이미지", "image"])
                        : (() => {
                            const m = value.match(/[1-4]/);
                            const n = m ? m[0] : value.replace("x", "");
                            return ["x" + n, n + "x", n];
                        })();
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 18 && r.height >= 12 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const normalized = (text) => String(text || "").replace(/\\s+/g, " ").trim().toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],[role='tab'],div,span"));
                    const vh = Math.max(1, window.innerHeight || 900);
                    const panelTop = inputBox ? Math.max(0, inputBox.y - 520) : 40;
                    const panelBottom = inputBox ? Math.min(vh, inputBox.y + inputBox.h + 120) : vh;
                    const panelLeft = inputBox ? Math.max(80, inputBox.x + inputBox.w * 0.10) : 120;
                    const panelRight = inputBox ? Math.min(window.innerWidth || 1600, inputBox.x + inputBox.w + 120) : 1000;
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.y < panelTop || r.y > panelBottom || r.x < panelLeft || r.x > panelRight) continue;
                        if (r.width > 360 || r.height > 100) continue;
                        const text = normalized([el.innerText, el.textContent, el.getAttribute("aria-label"), el.getAttribute("title")].join(" "));
                        if (!text) continue;
                        let hit = false;
                        let exact = false;
                        for (const t of targetTexts) {
                            if (!t) continue;
                            if (text === t || text.split(" ").includes(t)) {
                                hit = true;
                                exact = true;
                            } else if (kind === "media" && text.includes(t)) {
                                hit = true;
                            }
                        }
                        if (!hit) continue;
                        if (kind === "variant" && !new RegExp("\\\\b(?:x[1-4]|[1-4]x)\\\\b").test(text) && !/^(?:x?[1-4]|[1-4]x)$/.test(text)) continue;
                        if (/arrow_forward|생성|generate|검색|search|필터|정렬|dashboard|도움|help/.test(text)) continue;
                        const role = (el.getAttribute("role") || "").toLowerCase();
                        const tag = (el.tagName || "").toLowerCase();
                        let score = 600;
                        if (tag === "button") score += 160;
                        if (role === "button" || role === "tab") score += 130;
                        if (exact) score += 280;
                        if (kind === "media") {
                            if (r.width >= 80 && r.width <= 180) score += 80;
                            if (inputBox) score -= Math.abs((r.y + r.height / 2) - (inputBox.y - 250)) * 0.25;
                        } else {
                            if (r.width >= 40 && r.width <= 120) score += 110;
                            if (inputBox) score -= Math.abs((r.y + r.height / 2) - (inputBox.y - 125)) * 0.15;
                        }
                        rows.push({score, text:text.slice(0, 140), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a, b) => b.score - a.score);
                    if (!best || bestScore < 120) return {ok:false, rows:rows.slice(0, 10)};
                    document.querySelectorAll("[data-flow-worker-panel-choice]").forEach((el) => el.removeAttribute("data-flow-worker-panel-choice"));
                    best.setAttribute("data-flow-worker-panel-choice", marker);
                    return {ok:true, marker, rows:rows.slice(0, 10)};
                }""",
                {"marker": marker, "kind": choice_type, "value": value, "inputBox": input_box},
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 생성 옵션 선택 후보 탐지 실패: {choice_type}/{value} | {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), choice_type, str(row.get("text") or ""), box))
        self._dump_candidate_rows(f"생성 옵션 선택 후보 상위 ({choice_type}={value})", rows[:6])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-panel-choice='{marker}']").first

    def _dump_generation_panel_candidates(self, choice_type: str, value: str, input_locator=None) -> None:
        if not self.page:
            return
        input_box = self._safe_box(input_locator) if input_locator is not None else None
        try:
            payload = self.page.evaluate(
                """(inputBox) => {
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 10 && r.height >= 10 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const top = inputBox ? Math.max(0, inputBox.y - 540) : 0;
                    const bottom = inputBox ? inputBox.y + inputBox.h + 130 : window.innerHeight;
                    return Array.from(document.querySelectorAll("button,[role='button'],[role='tab'],div,span"))
                        .map((el) => {
                            const r = el.getBoundingClientRect();
                            return {
                                vis:isVisible(el), x:r.x, y:r.y, w:r.width, h:r.height,
                                meta:[el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").slice(0, 180)
                            };
                        })
                        .filter((row) => row.vis && row.y >= top && row.y <= bottom && /이미지|동영상|video|image|nano|banana|veo|x1|x2|x3|x4|1x|2x|3x|4x|16:9|9:16|4:3|3:4|1:1|프레임|에셋/i.test(row.meta))
                        .slice(0, 40);
                }""",
                input_box,
            ) or []
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 생성 옵션 후보 덤프 실패: {exc}")
            return
        self._action_log(f"[{self._clock()}] 생성 옵션 후보 덤프 | {choice_type}={value}")
        for idx, row in enumerate(payload, start=1):
            self._action_log(
                f"[{self._clock()}]   OPT {idx:02d}. meta='{str(row.get('meta') or '')[:160]}' "
                f"box=({float(row.get('x') or 0.0):.1f},{float(row.get('y') or 0.0):.1f},"
                f"{float(row.get('w') or 0.0):.1f},{float(row.get('h') or 0.0):.1f})"
            )

    def _open_result_detail(self, tag: str, log: LogFn) -> None:
        tag = self._normalize_reference_tag(tag)
        self._click_download_filter("video")
        search = self._resolve_top_search_input(timeout_sec=8)
        if search is None:
            raise RuntimeError("확장할 영상 검색 입력칸을 찾지 못했습니다.")
        self._fill_search_input(search, tag)
        self._action_log(f"[{self._clock()}] 확장 대상 검색 입력: {tag}")
        time.sleep(1.0)
        card = self._resolve_result_card(tag, timeout_sec=14)
        if card is None:
            raise RuntimeError(f"확장 대상 카드 탐지 실패: {tag}")
        try:
            self.actor.move_to_locator(card, label=f"확장 대상 카드({tag})")
        except Exception:
            try:
                card.hover(timeout=1200)
            except Exception:
                pass
        if not self._click_with_actor_fallback(card, f"확장 대상 카드({tag})"):
            card.click(timeout=2500, force=True)
        log(f"🎞️ 확장 대상 열기: {tag}")
        self._action_log(f"[{self._clock()}] 확장 대상 열기: {tag}")
        time.sleep(1.2)

    def _extend_current_video(self, *, log: LogFn) -> None:
        model = str(self.cfg.get("video_extension_model") or "Veo 3.1 - Fast").strip() or "Veo 3.1 - Fast"
        self._select_extension_model(model, log=log)
        extend_button = self._resolve_extend_button()
        if extend_button is None:
            self._dump_detail_action_candidates("확장 버튼 탐지 실패")
            raise RuntimeError("현재 영상 화면에서 확장 버튼을 찾지 못했습니다.")
        if not self._click_with_actor_fallback(extend_button, "확장 버튼"):
            extend_button.click(timeout=2500, force=True)
        log(f"⏩ 현재 영상 확장 시작: {model}")
        self._action_log(f"[{self._clock()}] 현재 영상 확장 시작: {model}")
        time.sleep(0.8)

    def _select_extension_model(self, model: str, *, log: LogFn) -> None:
        dropdown = self._resolve_extension_model_dropdown()
        if dropdown is None:
            self._action_log(f"[{self._clock()}] 확장 모델 드롭다운 미탐지: {model}")
            return
        meta = self._locator_meta(dropdown)
        if self._model_text_matches(meta, model):
            log(f"🎛️ 확장 모델 유지: {model}")
            self._action_log(f"[{self._clock()}] 확장 모델 유지: {model}")
            return
        if not self._click_with_actor_fallback(dropdown, "확장 모델 선택"):
            dropdown.click(timeout=2500, force=True)
        time.sleep(0.45)
        option = self._resolve_model_option(model)
        if option is None:
            self._dump_detail_action_candidates(f"확장 모델 옵션 탐지 실패: {model}")
            raise RuntimeError(f"확장 모델 옵션을 찾지 못했습니다: {model}")
        if not self._click_with_actor_fallback(option, f"확장 모델 {model}"):
            option.click(timeout=2500, force=True)
        log(f"🎛️ 확장 모델 선택: {model}")
        self._action_log(f"[{self._clock()}] 확장 모델 선택: {model}")
        time.sleep(0.4)

    @staticmethod
    def _model_text_matches(meta: str, model: str) -> bool:
        source = str(meta or "").replace(" ", "").lower()
        target = str(model or "").replace(" ", "").lower()
        if target and target in source:
            return True
        return "veo3.1" in source and "fast" in source if "fast" in target else False

    def _resolve_extension_model_dropdown(self):
        if not self.page:
            return None
        marker = f"flow-worker-extension-model-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        try:
            result = self.page.evaluate(
                """(marker) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 70 && r.height >= 24 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],[role='combobox'],div,span"));
                    const vw = window.innerWidth || 1280;
                    const vh = window.innerHeight || 900;
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = metaText(el);
                        if (!/veo|lite|fast|quality/i.test(meta)) continue;
                        if (/download|다운로드|공유|share|좋아요|like|확장|삽입|삭제|카메라/.test(meta)) continue;
                        if (r.width > 360 || r.height > 90) continue;
                        let score = 500;
                        if (/veo\\s*3\\.1|veo3\\.1/.test(meta)) score += 450;
                        if (/fast|lite|quality/.test(meta)) score += 180;
                        if (r.y > vh * 0.55) score += 180;
                        if (r.x > vw * 0.38 && r.x < vw * 0.78) score += 130;
                        score -= Math.abs((r.x + r.width / 2) - vw * 0.58) * 0.12;
                        score -= Math.abs((r.y + r.height / 2) - vh * 0.88) * 0.18;
                        rows.push({score, meta:meta.slice(0, 140), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a,b) => b.score - a.score);
                    if (!best || bestScore < 300) return {ok:false, rows:rows.slice(0, 8)};
                    document.querySelectorAll("[data-flow-worker-extension-model]").forEach((el) => el.removeAttribute("data-flow-worker-extension-model"));
                    best.setAttribute("data-flow-worker-extension-model", marker);
                    return {ok:true, marker, rows:rows.slice(0, 8)};
                }""",
                marker,
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 확장 모델 드롭다운 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "model", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("확장 모델 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-extension-model='{marker}']").first

    def _resolve_model_option(self, model: str):
        targets = [str(model or "").strip().lower(), "veo 3.1 - fast", "veo 3.1 fast", "fast"]
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                loc = self.page.locator("button,[role='option'],[role='menuitem'],[role='button'],div,span")
                total = min(loc.count(), 160)
            except Exception:
                return None
            for idx in range(total):
                cand = loc.nth(idx)
                try:
                    if not cand.is_visible(timeout=120):
                        continue
                except Exception:
                    continue
                box = self._safe_box(cand)
                if not box or box["w"] < 80.0 or box["h"] < 20.0:
                    continue
                meta = self._locator_meta(cand)
                compact = meta.replace(" ", "")
                if any(target and (target in meta or target.replace(" ", "") in compact) for target in targets):
                    if "fast" in meta and "lower priority" not in meta:
                        return cand
            time.sleep(0.2)
        return None

    def _resolve_extend_button(self):
        if not self.page:
            return None
        marker = f"flow-worker-extend-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        try:
            result = self.page.evaluate(
                """(marker) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 52 && r.height >= 28 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],div,span"));
                    const vw = window.innerWidth || 1280;
                    const vh = window.innerHeight || 900;
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = metaText(el);
                        if (!/확장|extend/.test(meta)) continue;
                        if (/기록|history|검색|search|삭제|delete|삽입|insert|카메라|camera/.test(meta)) continue;
                        if (r.width > 220 || r.height > 90) continue;
                        let score = 700;
                        if (r.y > vh * 0.72) score += 260;
                        if (r.x < vw * 0.45) score += 160;
                        if ((el.tagName || "").toLowerCase() === "button") score += 130;
                        if ((el.getAttribute("role") || "").toLowerCase() === "button") score += 80;
                        score -= Math.abs((r.y + r.height / 2) - vh * 0.94) * 0.22;
                        rows.push({score, meta:meta.slice(0, 140), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a,b) => b.score - a.score);
                    if (!best || bestScore < 450) return {ok:false, rows:rows.slice(0, 8)};
                    document.querySelectorAll("[data-flow-worker-extend]").forEach((el) => el.removeAttribute("data-flow-worker-extend"));
                    best.setAttribute("data-flow-worker-extend", marker);
                    return {ok:true, marker, rows:rows.slice(0, 8)};
                }""",
                marker,
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 확장 버튼 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "extend", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("확장 버튼 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-extend='{marker}']").first

    def _download_current_detail_video(self, *, tag: str, quality: str, log: LogFn) -> str:
        output_dir = Path(str(self.cfg.get("download_output_dir") or "").strip() or str((self.base_dir / "downloads").resolve()))
        output_dir.mkdir(parents=True, exist_ok=True)
        tag = self._normalize_reference_tag(tag)
        button = self._resolve_detail_download_button()
        if button is None:
            self._dump_detail_action_candidates("상세 다운로드 버튼 탐지 실패")
            return self._download_result(tag=tag, quality=quality, log=log)
        try:
            with self.page.expect_download(timeout=7000) as download_info:
                if not self._click_with_actor_fallback(button, "상세 다운로드 버튼"):
                    button.click(timeout=2500, force=True)
            download = download_info.value
        except Exception:
            quality_item = self._resolve_quality_menu_item(quality, timeout_sec=5)
            if quality_item is None:
                return self._download_result(tag=tag, quality=quality, log=log)
            with self.page.expect_download(timeout=int(self._download_expect_timeout_sec("video", quality) * 1000)) as download_info:
                if not self._click_with_actor_fallback(quality_item, f"{quality} 품질"):
                    quality_item.click(timeout=2500, force=True)
            download = download_info.value
        suggested = str(download.suggested_filename or "").strip() or f"{tag}.mp4"
        ext = Path(suggested).suffix or ".mp4"
        save_path = self._next_available_path(output_dir / self._ensure_safe_filename(f"{tag}{ext}"))
        download.save_as(str(save_path))
        log(f"💾 확장 영상 다운로드 완료: {save_path.name}")
        self._action_log(f"[{self._clock()}] 확장 영상 다운로드 저장: {save_path}")
        self._extract_next_start_frame_if_enabled(tag=tag, video_path=save_path, output_dir=output_dir, log=log)
        return save_path.name

    def _resolve_detail_download_button(self):
        if not self.page:
            return None
        marker = f"flow-worker-detail-download-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        try:
            result = self.page.evaluate(
                """(marker) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return r.width >= 20 && r.height >= 20 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").toLowerCase();
                    const nodes = Array.from(document.querySelectorAll("button,[role='button'],div,span"));
                    const vw = window.innerWidth || 1280;
                    const vh = window.innerHeight || 900;
                    let best = null;
                    let bestScore = -1e9;
                    const rows = [];
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const r = el.getBoundingClientRect();
                        const meta = metaText(el);
                        if (!/download|다운로드|file_download|arrow_downward/.test(meta)) continue;
                        if (/검색|search|필터|filter/.test(meta)) continue;
                        if (r.width > 90 || r.height > 90) continue;
                        let score = 650;
                        if (r.y < vh * 0.18) score += 220;
                        if (r.x > vw * 0.70) score += 180;
                        if ((el.tagName || "").toLowerCase() === "button") score += 100;
                        score -= Math.abs((r.x + r.width / 2) - vw * 0.82) * 0.18;
                        score -= Math.abs((r.y + r.height / 2) - 38) * 0.45;
                        rows.push({score, meta:meta.slice(0, 140), x:r.x, y:r.y, w:r.width, h:r.height});
                        if (score > bestScore) {
                            bestScore = score;
                            best = el;
                        }
                    }
                    rows.sort((a,b) => b.score - a.score);
                    if (!best || bestScore < 430) return {ok:false, rows:rows.slice(0, 8)};
                    document.querySelectorAll("[data-flow-worker-detail-download]").forEach((el) => el.removeAttribute("data-flow-worker-detail-download"));
                    best.setAttribute("data-flow-worker-detail-download", marker);
                    return {ok:true, marker, rows:rows.slice(0, 8)};
                }""",
                marker,
            ) or {}
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 상세 다운로드 버튼 탐지 실패: {exc}")
            return None
        rows = []
        for row in list(result.get("rows") or []):
            box = {"x": row.get("x", 0), "y": row.get("y", 0), "w": row.get("w", 0), "h": row.get("h", 0)}
            rows.append((float(row.get("score") or 0.0), "detail-download", str(row.get("meta") or ""), box))
        self._dump_candidate_rows("상세 다운로드 후보 상위", rows[:5])
        if not result.get("ok"):
            return None
        return self.page.locator(f"[data-flow-worker-detail-download='{marker}']").first

    def _dump_detail_action_candidates(self, label: str) -> None:
        if not self.page:
            return
        try:
            rows = self.page.evaluate(
                """() => Array.from(document.querySelectorAll("button,[role='button'],[role='option'],[role='menuitem'],div,span"))
                    .map((el) => {
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        return {
                            vis:r.width >= 10 && r.height >= 10 && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0",
                            x:r.x, y:r.y, w:r.width, h:r.height,
                            meta:[el.tagName||"", el.getAttribute("role")||"", el.getAttribute("aria-label")||"", el.getAttribute("title")||"", el.innerText||"", el.textContent||""].join(" ").replace(/\\s+/g, " ").slice(0, 180)
                        };
                    })
                    .filter((row) => row.vis && /확장|extend|veo|fast|lite|quality|download|다운로드|삽입|카메라/i.test(row.meta))
                    .slice(0, 50)"""
            ) or []
        except Exception as exc:
            self._action_log(f"[{self._clock()}] 상세 액션 후보 덤프 실패: {exc}")
            return
        self._action_log(f"[{self._clock()}] {label}")
        for idx, row in enumerate(rows, start=1):
            self._action_log(
                f"[{self._clock()}]   DETAIL {idx:02d}. meta='{str(row.get('meta') or '')[:160]}' "
                f"box=({float(row.get('x') or 0.0):.1f},{float(row.get('y') or 0.0):.1f},"
                f"{float(row.get('w') or 0.0):.1f},{float(row.get('h') or 0.0):.1f})"
            )

    def _download_result(self, *, tag: str, quality: str, log: LogFn) -> str:
        output_dir = Path(str(self.cfg.get("download_output_dir") or "").strip() or str((self.base_dir / "downloads").resolve()))
        output_dir.mkdir(parents=True, exist_ok=True)
        media_mode = str(self.cfg.get("media_mode") or "image").strip().lower()
        media_mode = "video" if media_mode == "video" else "image"
        tag = self._normalize_reference_tag(tag)
        quality = str(quality or self._download_quality(media_mode)).strip().upper()

        self._click_download_filter(media_mode)
        search = self._resolve_top_search_input(timeout_sec=8)
        if search is None:
            raise RuntimeError("다운로드 검색 입력칸을 찾지 못했습니다.")
        self._fill_search_input(search, tag)
        self._action_log(f"[{self._clock()}] 다운로드 검색 입력: {tag}")
        time.sleep(1.0)

        card = self._resolve_result_card(tag, timeout_sec=14)
        if card is None:
            raise RuntimeError(f"다운로드 카드 탐지 실패: {tag}")
        try:
            self.actor.move_to_locator(card, label=f"다운로드 카드({tag})")
        except Exception:
            try:
                card.hover(timeout=1200)
            except Exception:
                pass
        time.sleep(0.18)

        more_btn = self._resolve_card_more_button(card)
        if more_btn is None:
            raise RuntimeError(f"다운로드 더보기 버튼 탐지 실패: {tag}")
        if not self._click_with_actor_fallback(more_btn, "다운로드 더보기 버튼"):
            raise RuntimeError(f"다운로드 더보기 버튼 클릭 실패: {tag}")
        self._action_log(f"[{self._clock()}] 다운로드 더보기 클릭 시도: {tag}")
        time.sleep(0.45)

        menu_item = self._resolve_download_menu_item(timeout_sec=5)
        if menu_item is None:
            more_btn = self._resolve_card_more_button(card)
            if more_btn is not None:
                self._click_with_actor_fallback(more_btn, "다운로드 더보기 버튼 재시도")
                time.sleep(0.35)
                menu_item = self._resolve_download_menu_item(timeout_sec=3)
        if menu_item is None:
            raise RuntimeError("다운로드 메뉴 탐지 실패")
        try:
            self.actor.move_to_locator(menu_item, label="다운로드 메뉴")
            menu_item.hover(timeout=1200)
        except Exception:
            try:
                menu_item.hover(timeout=1200)
            except Exception:
                pass
        time.sleep(0.35)

        quality_item = self._resolve_quality_menu_item(quality, timeout_sec=6)
        if quality_item is None:
            raise RuntimeError(f"다운로드 품질 메뉴 탐지 실패: {quality}")
        with self.page.expect_download(timeout=int(self._download_expect_timeout_sec(media_mode, quality) * 1000)) as download_info:
            if not self._click_with_actor_fallback(quality_item, f"{quality} 품질"):
                quality_item.click(timeout=2500, force=True)
        self._action_log(f"[{self._clock()}] 다운로드 품질 클릭: {tag} | {quality}")

        download = download_info.value
        suggested = str(download.suggested_filename or "").strip() or f"{tag}_{quality.lower()}"
        ext = Path(suggested).suffix or (".mp4" if media_mode == "video" else ".png")
        safe_name = self._ensure_safe_filename(f"{tag}{ext}")
        save_path = self._next_available_path(output_dir / safe_name)
        download.save_as(str(save_path))
        log(f"💾 다운로드 완료: {save_path.name}")
        self._action_log(f"[{self._clock()}] 다운로드 저장: {save_path}")
        if media_mode == "video":
            self._extract_next_start_frame_if_enabled(tag=tag, video_path=save_path, output_dir=output_dir, log=log)
        self._clear_search_input(search)
        return save_path.name

    def _extract_next_start_frame_if_enabled(self, *, tag: str, video_path: Path, output_dir: Path, log: LogFn) -> str:
        if not bool(self.cfg.get("video_auto_extract_last_frame", False)):
            return ""
        target = suggested_next_frame_path_for_tag(tag, output_dir)
        target = self._next_available_path(target)
        try:
            saved = extract_last_frame(video_path, target)
        except LastFrameExtractError as exc:
            log(f"⚠️ 마지막 프레임 자동 추출 실패: {exc}")
            self._action_log(f"[{self._clock()}] 마지막 프레임 자동 추출 실패: {exc}")
            return ""
        except Exception as exc:
            log(f"⚠️ 마지막 프레임 자동 추출 실패: {exc}")
            self._action_log(f"[{self._clock()}] 마지막 프레임 자동 추출 실패: {exc}")
            return ""
        log(f"🖼️ 다음 시작 프레임 자동 저장: {saved.name}")
        self._action_log(f"[{self._clock()}] 다음 시작 프레임 자동 저장: {saved}")
        return saved.name

    def _download_search_input_candidates(self) -> list[str]:
        selectors = [
            str(self.cfg.get("download_search_input_selector") or "").strip(),
            "input[placeholder*='검색' i]",
            "input[aria-label*='검색' i]",
            "input[placeholder*='search' i]",
            "input[aria-label*='search' i]",
            "input[type='search']",
            "[role='searchbox']",
            "[role='textbox'][aria-label*='검색' i]",
            "[role='textbox'][aria-label*='search' i]",
            "[contenteditable='true'][aria-label*='검색' i]",
            "[contenteditable='true'][aria-label*='search' i]",
            "input[type='text']",
            "input",
            "[role='textbox']",
        ]
        return [item for item in dict.fromkeys(item for item in selectors if item and item != "자동 탐색")]

    def _download_search_toggle_candidates(self) -> list[str]:
        return [
            "button[aria-label*='검색' i]",
            "[role='button'][aria-label*='검색' i]",
            "button[aria-label*='search' i]",
            "[role='button'][aria-label*='search' i]",
            "button[title*='search' i]",
            "button:has-text('search')",
            "[role='button']:has-text('search')",
        ]

    def _resolve_top_search_input(self, timeout_sec: float = 6.0):
        deadline = time.time() + max(1.0, timeout_sec)
        best = None
        best_score = float("-inf")
        toggled = False
        while time.time() < deadline:
            viewport_w, viewport_h = self._viewport_size()
            min_x = max(70.0, viewport_w * 0.08)
            max_y = max(190.0, viewport_h * 0.42)
            rows: list[tuple[float, str, str, dict]] = []
            for sel in self._download_search_input_candidates():
                try:
                    loc = self.page.locator(sel)
                    total = min(loc.count(), 40)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=250):
                            continue
                    except Exception:
                        continue
                    box = self._safe_box(cand)
                    if not box:
                        continue
                    if box["w"] < 100.0 or box["h"] < 18.0:
                        continue
                    if box["y"] > max_y or box["x"] < min_x:
                        continue
                    meta = self._locator_meta(cand)
                    score = 0.0
                    if any(token in meta for token in ("검색", "search", "media", "all media", "find")):
                        score += 520.0
                    if any(token in meta for token in ("project", "title", "이름", "rename", "name", "prompt", "프롬프트", "무엇을 만들")):
                        score -= 1100.0
                    if box["w"] >= 280.0:
                        score += 220.0
                    elif box["w"] >= 180.0:
                        score += 80.0
                    else:
                        score -= 240.0
                    if box["y"] <= max(170.0, viewport_h * 0.24):
                        score += 140.0
                    else:
                        score -= 180.0
                    score -= abs((box["x"] + box["w"] / 2.0) - (viewport_w / 2.0)) * 0.18
                    rows.append((score, sel, meta[:120], box))
                    if score > best_score:
                        best = cand
                        best_score = score
            if best is not None and best_score >= -40.0:
                rows.sort(key=lambda row: row[0], reverse=True)
                self._dump_candidate_rows("다운로드 검색창 후보 상위", rows[:5])
                return best
            if not toggled:
                toggle = self._resolve_first_visible(self._download_search_toggle_candidates(), timeout_ms=600)
                if toggle is not None and self._click_with_actor_fallback(toggle, "검색 아이콘"):
                    toggled = True
                    time.sleep(0.35)
            time.sleep(0.25)
        return best

    def _fill_search_input(self, locator, tag: str) -> None:
        try:
            locator.click(timeout=1200)
        except Exception:
            pass
        try:
            locator.press("Control+A", timeout=1000)
            locator.press("Backspace", timeout=1000)
        except Exception:
            pass
        try:
            locator.fill(tag, timeout=1200)
        except Exception:
            locator.type(tag, delay=35, timeout=1200)
        try:
            locator.press("Enter", timeout=800)
        except Exception:
            try:
                self.page.keyboard.press("Enter")
            except Exception:
                pass

    def _clear_search_input(self, locator) -> None:
        if locator is None:
            return
        try:
            locator.click(timeout=800)
            locator.press("Control+A", timeout=800)
            locator.press("Backspace", timeout=800)
        except Exception:
            pass

    def _download_filter_candidates(self, media_mode: str) -> list[str]:
        if media_mode == "video":
            selectors = [
                "button[aria-label*='영상' i]",
                "[role='button'][aria-label*='영상' i]",
                "button[aria-label*='video' i]",
                "[role='button'][aria-label*='video' i]",
                "button:has-text('Video')",
                "[role='button']:has-text('Video')",
                "button:has-text('videocam')",
                "[role='button']:has-text('videocam')",
            ]
        else:
            selectors = [
                "button[aria-label*='이미지' i]",
                "[role='button'][aria-label*='이미지' i]",
                "button[aria-label*='image' i]",
                "[role='button'][aria-label*='image' i]",
                "button:has-text('Image')",
                "[role='button']:has-text('Image')",
            ]
        selectors.extend(["button", "[role='button']", "div[role='button']"])
        return [item for item in dict.fromkeys(selectors)]

    def _click_download_filter(self, media_mode: str) -> None:
        button = self._resolve_download_filter_button(media_mode)
        if button is None:
            self._action_log(f"[{self._clock()}] 다운로드 필터 버튼 미탐지: {media_mode}")
            return
        if self._click_with_actor_fallback(button, f"다운로드 {media_mode} 필터"):
            self._action_log(f"[{self._clock()}] 다운로드 필터 클릭: {media_mode}")
            time.sleep(0.35)

    def _resolve_download_filter_button(self, media_mode: str):
        viewport_w, viewport_h = self._viewport_size()
        target_y = 235.0 if media_mode == "video" else 175.0
        best = None
        best_score = float("-inf")
        for sel in self._download_filter_candidates(media_mode):
            try:
                loc = self.page.locator(sel)
                total = min(loc.count(), 80)
            except Exception:
                continue
            for idx in range(total):
                cand = loc.nth(idx)
                try:
                    if not cand.is_visible(timeout=180):
                        continue
                except Exception:
                    continue
                box = self._safe_box(cand)
                if not box:
                    continue
                if box["x"] > max(135.0, viewport_w * 0.14):
                    continue
                if box["y"] < 60.0 or box["y"] > viewport_h * 0.55:
                    continue
                if box["w"] < 18.0 or box["h"] < 18.0 or box["w"] > 100.0 or box["h"] > 100.0:
                    continue
                meta = self._locator_meta(cand)
                cy = box["y"] + box["h"] / 2.0
                score = 900.0 - (box["x"] * 1.4) - abs(cy - target_y) * 2.4
                if media_mode == "video":
                    if any(k in meta for k in ("video", "영상", "동영상", "videocam")):
                        score += 320.0
                    if any(k in meta for k in ("image", "이미지", "photo", "사진")):
                        score -= 520.0
                else:
                    if any(k in meta for k in ("image", "이미지", "photo", "사진")):
                        score += 320.0
                    if any(k in meta for k in ("video", "영상", "동영상", "videocam")):
                        score -= 520.0
                if any(k in meta for k in ("upload", "업로드", "download", "다운로드", "search", "검색", "back", "뒤로", "menu", "메뉴")):
                    score -= 680.0
                if abs(box["w"] - box["h"]) <= 14.0:
                    score += 45.0
                if score > best_score:
                    best = cand
                    best_score = score
        return best if best_score > 120.0 else None

    def _resolve_result_card(self, tag: str, timeout_sec: float = 8.0):
        normalized = self._normalize_reference_tag(tag)
        deadline = time.time() + max(1.0, timeout_sec)
        selectors = [
            "article",
            "[role='listitem']",
            "li",
            "div[class*='card' i]",
            "div[class*='tile' i]",
            "div[data-testid*='card' i]",
            "div[data-testid*='result' i]",
            "div[role='button']",
            "div",
        ]
        best_rows: list[tuple[float, str, str, dict]] = []
        while time.time() < deadline:
            viewport_w, viewport_h = self._viewport_size()
            best = None
            best_score = float("-inf")
            rows: list[tuple[float, str, str, dict]] = []
            for sel in selectors:
                try:
                    loc = self.page.locator(sel)
                    total = min(loc.count(), 160 if sel == "div" else 60)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if not cand.is_visible(timeout=160):
                            continue
                    except Exception:
                        continue
                    box = self._safe_box(cand)
                    if not box or box["w"] < 140.0 or box["h"] < 90.0:
                        continue
                    if box["y"] < 85.0 or box["y"] > viewport_h - 70.0:
                        continue
                    if box["x"] < 45.0 or box["x"] > viewport_w - 120.0:
                        continue
                    if box["w"] > viewport_w * 0.72 or box["h"] > viewport_h * 0.74:
                        continue
                    meta = self._locator_meta(cand)
                    score = 0.0
                    if normalized and normalized.lower() in meta:
                        score += 900.0
                    if any(token in meta for token in ("veo", "download", "다운로드", "1k", "720p", "1080p", "nano", "banana")):
                        score += 90.0
                    if any(token in meta for token in ("무엇을 만들", "prompt", "프롬프트", "검색", "search", "project", "프로젝트")):
                        score -= 520.0
                    aspect = box["w"] / max(1.0, box["h"])
                    if 0.85 <= aspect <= 2.35:
                        score += 160.0
                    score += max(0.0, 520.0 - box["y"]) * 0.35
                    score -= box["x"] * 0.05
                    rows.append((score, sel, meta[:120], box))
                    if score > best_score:
                        best = cand
                        best_score = score
            if rows:
                rows.sort(key=lambda row: row[0], reverse=True)
                best_rows = rows[:6]
            if best is not None and best_score > 120.0:
                self._dump_candidate_rows("다운로드 카드 후보 상위", best_rows[:5])
                return best
            time.sleep(0.45)
        self._dump_candidate_rows("다운로드 카드 후보 실패", best_rows[:6])
        return None

    def _resolve_card_more_button(self, card):
        try:
            card_box = card.bounding_box()
        except Exception:
            card_box = None
        if not card_box:
            return None
        try:
            card.hover(timeout=1200)
        except Exception:
            pass
        time.sleep(0.12)
        best = None
        best_score = float("inf")
        anchor_x = float(card_box["x"]) + float(card_box["width"]) - 26.0
        anchor_y = float(card_box["y"]) + 20.0
        pools = []
        try:
            pools.append(card.locator("button, [role='button']"))
        except Exception:
            pass
        try:
            pools.append(self.page.locator("button, [role='button']"))
        except Exception:
            pass
        for pool in pools:
            try:
                total = min(pool.count(), 120)
            except Exception:
                continue
            for idx in range(total):
                cand = pool.nth(idx)
                try:
                    if not cand.is_visible(timeout=160):
                        continue
                except Exception:
                    continue
                box = self._safe_box(cand)
                if not box:
                    continue
                cx = box["x"] + box["w"] * 0.5
                cy = box["y"] + box["h"] * 0.5
                if cx < float(card_box["x"]) - 20.0 or cx > float(card_box["x"]) + float(card_box["width"]) + 45.0:
                    continue
                if cy < float(card_box["y"]) - 35.0 or cy > float(card_box["y"]) + min(130.0, float(card_box["height"]) * 0.45):
                    continue
                score = abs(cx - anchor_x) + abs(cy - anchor_y)
                meta = self._locator_meta(cand)
                if any(token in meta for token in ("더보기", "more", "menu", "⋮", "...", "options")):
                    score -= 260.0
                if any(token in meta for token in ("play", "재생", "download", "다운로드", "image", "video", "검색", "search")):
                    score += 260.0
                if score < best_score:
                    best = cand
                    best_score = score
        return best

    def _resolve_download_menu_item(self, timeout_sec: float = 4.0):
        selectors = [
            "button:has-text('다운로드')",
            "[role='menuitem']:has-text('다운로드')",
            "[role='button']:has-text('다운로드')",
            "text=다운로드",
            "button:has-text('Download')",
            "[role='menuitem']:has-text('Download')",
            "[role='button']:has-text('Download')",
            "text=Download",
        ]
        return self._wait_first_visible(selectors, timeout_sec=timeout_sec)

    def _resolve_quality_menu_item(self, quality: str, timeout_sec: float = 4.0):
        targets = [str(quality or "").strip().upper()]
        if targets[0] == "1080P":
            targets.append("1080")
        if targets[0] == "1K":
            targets.append("1 k")
        selectors = [
            "button",
            "[role='button']",
            "[role='menuitem']",
            "[role='option']",
            "li",
            "div[role='button']",
        ]
        deadline = time.time() + max(0.6, timeout_sec)
        while time.time() < deadline:
            for target in targets:
                if not target:
                    continue
                for sel in selectors:
                    try:
                        loc = self.page.locator(sel)
                        total = min(loc.count(), 100)
                    except Exception:
                        continue
                    for idx in range(total):
                        cand = loc.nth(idx)
                        try:
                            if not cand.is_visible(timeout=150):
                                continue
                        except Exception:
                            continue
                        meta = self._locator_meta(cand)
                        compact_meta = meta.replace(" ", "")
                        compact_target = target.lower().replace(" ", "")
                        if target.lower() in meta or compact_target in compact_meta:
                            return cand
            time.sleep(0.2)
        return None

    def _resolve_first_visible(self, selectors: list[str], timeout_ms: int = 800):
        deadline = time.time() + max(0.1, timeout_ms / 1000.0)
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = self.page.locator(sel)
                    total = min(loc.count(), 20)
                except Exception:
                    continue
                for idx in range(total):
                    cand = loc.nth(idx)
                    try:
                        if cand.is_visible(timeout=120):
                            return cand
                    except Exception:
                        continue
            time.sleep(0.08)
        return None

    def _wait_first_visible(self, selectors: list[str], timeout_sec: float = 4.0):
        deadline = time.time() + max(0.2, timeout_sec)
        while time.time() < deadline:
            found = self._resolve_first_visible(selectors, timeout_ms=350)
            if found is not None:
                return found
            time.sleep(0.12)
        return None

    def _download_expect_timeout_sec(self, media_mode: str, quality: str) -> int:
        if media_mode == "video":
            return 150 if str(quality).upper() in {"1080P", "4K"} else 90
        return 90 if str(quality).upper() in {"2K", "4K"} else 60

    def _next_available_path(self, path_obj: Path) -> Path:
        if not path_obj.exists():
            return path_obj
        stem = path_obj.stem
        suffix = path_obj.suffix
        parent = path_obj.parent
        for n in range(1, 10000):
            candidate = parent / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                return candidate
        return parent / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"

    def _viewport_size(self) -> tuple[float, float]:
        try:
            vp = self.page.viewport_size or {}
            return float(vp.get("width") or 1600.0), float(vp.get("height") or 900.0)
        except Exception:
            return 1600.0, 900.0

    def _download_quality(self, media_mode: str) -> str:
        if media_mode == "video":
            return str(self.cfg.get("video_quality") or "720P").strip().upper() or "720P"
        return str(self.cfg.get("image_quality") or "1K").strip().upper() or "1K"

    def _wait_if_paused(self, set_status: StatusFn, is_paused: PauseFn, should_stop: StopFn) -> None:
        while is_paused():
            if should_stop():
                return
            set_status("일시정지")
            time.sleep(0.25)

    def _timed_wait(self, seconds: float, should_stop: StopFn, is_paused: PauseFn, set_status: StatusFn, *, label: str) -> None:
        deadline = time.time() + max(0.0, float(seconds or 0.0))
        while time.time() < deadline:
            if should_stop():
                return
            while is_paused():
                if should_stop():
                    return
                set_status("일시정지")
                time.sleep(0.25)
            time.sleep(0.25)
            remain = max(0.0, deadline - time.time())
            set_status(f"{label} {int(remain + 0.999)}초")

    def _open_action_log(self, log: LogFn) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_dir = self.base_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        self._action_log_path = logs_dir / f"action_trace_{stamp}.log"
        self._action_log_path.write_text(f"[{self._clock()}] 액션 로그 파일 생성: {self._action_log_path}\n", encoding="utf-8")
        log(f"🧾 액션 로그 파일 생성: {self._action_log_path}")

    def _action_log(self, line: str) -> None:
        if self._action_log_path is None:
            return
        try:
            with self._action_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except Exception:
            return

    @staticmethod
    def _clock() -> str:
        return datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def _normalize_reference_tag(raw: str) -> str:
        text = str(raw or "").strip().upper()
        match = re.match(r"^[A-Z]*0*([1-9][0-9]*)$", text)
        if not match:
            return text
        return f"S{int(match.group(1)):03d}"

    @staticmethod
    def _char_delay(ch: str) -> float:
        if ch in (" ", "\n"):
            return random.uniform(0.008, 0.03)
        if ch in ".,!?:;)(":
            return random.uniform(0.01, 0.04)
        return random.uniform(0.012, 0.05)

    @staticmethod
    def _ensure_safe_filename(name: str) -> str:
        text = re.sub(r'[<>:"/\\\\|?*]+', "_", str(name or "").strip())
        return text[:180] or "download.bin"

    @staticmethod
    def _locator_meta(locator) -> str:
        try:
            value = locator.evaluate(
                """(el) => [
                    el.tagName || "",
                    el.id || "",
                    el.className || "",
                    el.getAttribute("name") || "",
                    el.getAttribute("placeholder") || "",
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                    el.innerText || "",
                    el.textContent || "",
                ].join(" ").toLowerCase()"""
            )
            return str(value or "").strip().lower()
        except Exception:
            return ""

    def _read_input_text(self, locator) -> str:
        if locator is None:
            return ""
        try:
            value = locator.evaluate(
                """(el) => {
                    if (!el) return "";
                    if ("value" in el && typeof el.value === "string") return el.value;
                    return (el.innerText || el.textContent || "");
                }"""
            )
            return str(value or "").strip()
        except Exception:
            return ""

    def _safe_box(self, locator) -> dict | None:
        try:
            box = locator.bounding_box()
        except Exception:
            box = None
        if not box:
            return None
        try:
            return {
                "x": float(box.get("x") if isinstance(box, dict) else box["x"]),
                "y": float(box.get("y") if isinstance(box, dict) else box["y"]),
                "w": float(box.get("width") if isinstance(box, dict) else box["width"]),
                "h": float(box.get("height") if isinstance(box, dict) else box["height"]),
            }
        except Exception:
            return None

    def _dump_candidate_rows(self, title: str, rows: list[tuple[float, str, str, dict]]) -> None:
        if not rows:
            return
        self._action_log(f"[{self._clock()}] {title}")
        for idx, row in enumerate(rows, start=1):
            box = row[3] or {}
            self._action_log(
                f"[{self._clock()}]   {idx:02d}. score={row[0]:.1f} sel={row[1]} meta='{row[2]}' "
                f"box=({float(box.get('x') or 0.0):.1f},{float(box.get('y') or 0.0):.1f},"
                f"{float(box.get('w') or 0.0):.1f},{float(box.get('h') or 0.0):.1f})"
            )

    @staticmethod
    def _is_prompt_reference_overlay_input_box(box: dict | None) -> bool:
        if not box:
            return False
        try:
            width = float(box.get("w") or box.get("width") or 0.0)
            height = float(box.get("h") or box.get("height") or 0.0)
            x = float(box.get("x") or 0.0)
            y = float(box.get("y") or 0.0)
        except Exception:
            return False
        if width < 180.0 or width > 980.0:
            return False
        if height < 18.0 or height > 36.0:
            return False
        if y < 8.0 or y > 280.0:
            return False
        if x < 100.0 or x > 820.0:
            return False
        return True

    def _direct_fill_reference_search_via_dom(self, asset_tag: str) -> tuple[bool, str]:
        if not self.page or not asset_tag:
            return False, "page/tag 없음"
        try:
            result = self.page.evaluate(
                """(payload) => {
                    const tag = String(payload.tag || "").trim();
                    if (!tag) return {ok:false, reason:"empty-tag"};
                    const searchKeys = ["asset", "search", "에셋", "검색", "recent", "최근"];
                    const negativeKeys = ["무엇을 만들고 싶으신가요", "prompt", "프롬프트", "message", "메시지", "project", "title", "이름"];
                    const isVisible = (el) => {
                        const r = el.getBoundingClientRect();
                        if (!r || r.width < 10 || r.height < 10) return false;
                        const st = window.getComputedStyle(el);
                        return st && st.display !== "none" && st.visibility !== "hidden" && st.opacity !== "0";
                    };
                    const metaText = (el) => [
                        el.tagName || "", el.id || "", el.className || "",
                        el.getAttribute("name") || "", el.getAttribute("placeholder") || "",
                        el.getAttribute("aria-label") || "", el.getAttribute("title") || "",
                        el.innerText || "",
                    ].join(" ").toLowerCase();
                    let best = null;
                    let bestScore = -1e9;
                    const nodes = document.querySelectorAll("input, textarea, [role='searchbox'], [role='textbox'], [contenteditable='true']");
                    for (const el of nodes) {
                        if (!isVisible(el)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 180 || r.width > 980) continue;
                        if (r.height < 18 || r.height > 36) continue;
                        if (r.top < 8 || r.top > 160) continue;
                        if (r.left < 100 || r.left > 820) continue;
                        const meta = metaText(el);
                        let score = 0;
                        if (searchKeys.some(k => meta.includes(k))) score += 600;
                        if (negativeKeys.some(k => meta.includes(k))) score -= 1800;
                        if ((el.tagName || "").toLowerCase() === "input") score += 120;
                        score -= Math.abs((r.left + r.width / 2) - 420) * 0.22;
                        if (score > bestScore) {
                            best = el;
                            bestScore = score;
                        }
                    }
                    if (!best || bestScore < 120) return {ok:false, reason:"overlay-search-input-not-found"};
                    best.focus();
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
                }""",
                {"tag": asset_tag},
            )
            return bool(result and result.get("ok")), str((result or {}).get("reason") or "")
        except Exception as exc:
            return False, str(exc)

    def _click_with_actor_fallback(self, locator, label: str) -> bool:
        if locator is None:
            return False
        try:
            self.actor.move_to_locator(locator, label=label)
            self.actor.smart_click(label=f"{label} 클릭")
            return True
        except Exception:
            pass
        try:
            locator.click(timeout=2500)
            return True
        except Exception:
            return False

    def _capture_submit_state(self, submit_locator) -> dict:
        state = {"visible": False, "enabled": None, "meta": ""}
        if submit_locator is None:
            return state
        try:
            state["visible"] = bool(submit_locator.is_visible(timeout=250))
        except Exception:
            pass
        try:
            state["enabled"] = bool(submit_locator.is_enabled(timeout=250))
        except Exception:
            pass
        state["meta"] = self._locator_meta(submit_locator)
        return state

    def _is_generation_indicator_visible(self) -> bool:
        if not self.page:
            return False
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
                loc = self.page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=250):
                    return True
            except Exception:
                continue
        return False

    def _confirm_submission_started(
        self,
        input_locator,
        before_text: str,
        timeout_sec: float = 12.0,
        submit_locator=None,
        submit_before_state: dict | None = None,
        indicator_before: bool = False,
    ) -> tuple[bool, str]:
        deadline = time.time() + max(2.0, float(timeout_sec or 2.0))
        before_text = str(before_text or "").strip()
        min_shrunk_len = max(2, int(len(before_text) * 0.25)) if before_text else 2
        before_state = dict(submit_before_state or {})
        before_enabled = before_state.get("enabled")
        before_meta = str(before_state.get("meta") or "").strip().lower()
        while time.time() < deadline:
            current_state = self._capture_submit_state(submit_locator) if submit_locator is not None else {}
            current_meta = str(current_state.get("meta") or "").strip().lower()
            if submit_locator is not None:
                if before_enabled is True and current_state.get("enabled") is False:
                    return True, "submit_disabled"
                if current_meta and current_meta != before_meta and any(x in current_meta for x in ("중지", "취소", "stop", "cancel", "generating", "생성 중", "processing", "처리 중")):
                    return True, "submit_changed"
            if self._is_generation_indicator_visible() and not indicator_before:
                return True, "generation_indicator"
            current = self._read_input_text(input_locator)
            if before_text and current != before_text:
                if len(current) <= min_shrunk_len:
                    return True, "input_cleared"
                if len(current) < max(4, int(len(before_text) * 0.55)):
                    return True, "input_shrunk"
            time.sleep(0.5)
        return False, "timeout"
