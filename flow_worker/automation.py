from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .prompt_parser import PromptBlock, compress_numbers, load_prompt_blocks


LogFn = Callable[[str], None]
StatusFn = Callable[[str], None]
QueueFn = Callable[[int, str, str, str], None]
StopFn = Callable[[], bool]


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
    ) -> None:
        if not plan.items:
            set_status("선택된 작업 없음")
            return
        media_mode = str(self.cfg.get("media_mode") or "image").strip().lower()
        set_status("Flow 자동화 준비 중")
        log(f"🧪 새 Flow Worker 독립 엔진 실행 준비 | 모드={media_mode} | 선택={plan.selection_summary}")
        log("ℹ️ 현재 1차 버전은 백지 리포지토리 구조와 큐/브라우저 독립화까지 먼저 세운 상태입니다.")
        log("ℹ️ 다음 단계에서 실제 Flow 생성/다운로드 동작을 이 엔진에 직접 연결합니다.")
        for item in plan.items:
            if should_stop():
                set_status("중지됨")
                log("⏹️ 사용자 중지 요청으로 작업을 멈췄습니다.")
                return
            update_queue(item.number, "pending", f"{item.tag} 준비됨", "")
        set_status("엔진 뼈대 준비 완료")

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
