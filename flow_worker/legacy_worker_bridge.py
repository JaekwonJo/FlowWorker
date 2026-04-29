from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .prompt_parser import PromptBlock, compress_numbers


class LegacyWorkerBridge:
    def __init__(self, base_dir: Path, log):
        self.base_dir = Path(base_dir)
        self.log = log
        self.runtime_dir = self.base_dir / "runtime" / "legacy_worker_bridge"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.process: subprocess.Popen | None = None
        self.mode = ""
        self.config_path: Path | None = None
        self.state_path: Path | None = None
        self.command_path: Path | None = None
        self.stdout_path: Path | None = None
        self.stderr_path: Path | None = None

    def ensure_backend(self, *, mode: str, ui_cfg: dict[str, Any], plan_items: list[PromptBlock] | None = None) -> None:
        mode = "asset" if str(mode or "").strip().lower() == "asset" else "prompt"
        if self.mode != mode and self.process and self.process.poll() is None:
            self.shutdown()
        self.mode = mode
        self.config_path = self.runtime_dir / f"worker_config_{mode}.json"
        self.state_path = self.runtime_dir / f"worker_state_{mode}.json"
        self.command_path = self.runtime_dir / f"worker_command_{mode}.json"
        self.stdout_path = self.runtime_dir / f"worker_stdout_{mode}.log"
        self.stderr_path = self.runtime_dir / f"worker_stderr_{mode}.log"
        self._write_worker_config(mode=mode, ui_cfg=ui_cfg, plan_items=plan_items or [])
        if self.process and self.process.poll() is None:
            self.send_action("reload_config")
            return
        self._launch_process(mode)
        self._wait_until_ready()

    def send_action(self, action: str) -> None:
        if not self.command_path:
            return
        payload = {"token": uuid.uuid4().hex, "action": str(action or "").strip().lower()}
        self.command_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_state(self) -> dict[str, Any] | None:
        if not self.state_path or (not self.state_path.exists()):
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def shutdown(self) -> None:
        try:
            self.send_action("shutdown")
        except Exception:
            pass
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.terminate()
                except Exception:
                    pass
        self.process = None
        self.mode = ""

    def _legacy_flow_root(self) -> Path:
        return self.base_dir.parent / "Flow Classic Plus" / "flow"

    def _legacy_flow_script(self) -> Path:
        return self._legacy_flow_root() / "flow_auto_v2.py"

    def _legacy_flow_config(self) -> Path:
        return self._legacy_flow_root() / "flow_config.json"

    def _read_legacy_base_config(self) -> dict[str, Any]:
        path = self._legacy_flow_config()
        if not path.exists():
            raise RuntimeError(f"원본 Flow 설정 파일이 없습니다: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("원본 Flow 설정 파일 형식이 올바르지 않습니다.")
        return payload

    def _write_worker_config(self, *, mode: str, ui_cfg: dict[str, Any], plan_items: list[PromptBlock]) -> None:
        expected_path = self.runtime_dir / f"worker_config_{mode}.json"
        if self.config_path is None or self.config_path.name != expected_path.name:
            self.config_path = self.runtime_dir / f"worker_config_{mode}.json"
        cfg = self._read_legacy_base_config()
        project = self._selected_project(ui_cfg)
        prompt_slot = self._selected_prompt_slot(ui_cfg)
        prompt_path = self.base_dir / str(prompt_slot.get("file") or "")
        temp_prompt_path = self.runtime_dir / f"worker_prompts_{mode}.txt"
        temp_prompt_path.write_text(self._build_runtime_prompt_text(mode=mode, items=plan_items, fallback_path=prompt_path), encoding="utf-8")

        cfg["worker_mode"] = mode
        cfg["worker_name"] = str(ui_cfg.get("worker_name") or "Flow Worker1")
        cfg["project_profiles"] = [{"project_name": str(project.get("name") or "기본 프로젝트"), "url": str(project.get("url") or "").strip()}]
        cfg["active_project_profile"] = 0
        cfg["start_url"] = str(project.get("url") or "").strip()
        cfg["prompt_slots"] = [{"name": str(prompt_slot.get("name") or "기본 프롬프트 파일"), "file": str(temp_prompt_path.resolve())}]
        cfg["active_prompt_slot"] = 0
        cfg["prompts_file"] = str(temp_prompt_path.resolve())
        cfg["browser_launch_mode"] = "edge_human"
        cfg["browser_channel"] = "msedge"
        cfg["browser_profile_dir"] = f"flowworker_{'video' if mode == 'asset' else 'image'}_profile"
        cfg["prompt_variant_count"] = str(ui_cfg.get("image_variant_count") or "x1").strip().lower() or "x1"
        cfg["asset_prompt_variant_count"] = str(ui_cfg.get("video_variant_count") or "x1").strip().lower() or "x1"
        cfg["download_image_quality"] = str(ui_cfg.get("image_quality") or "1K").strip().upper() or "1K"
        cfg["download_video_quality"] = str(ui_cfg.get("video_quality") or "1080P").strip().upper() or "1080P"
        wait_download = self._safe_int(ui_cfg.get("generate_wait_seconds"), default=10)
        wait_next = self._safe_int(ui_cfg.get("next_prompt_wait_seconds"), default=7)
        cfg["prompt_combined_download_wait_seconds"] = wait_download
        cfg["asset_combined_download_wait_seconds"] = wait_download
        cfg["prompt_combined_next_interval_seconds"] = wait_next
        cfg["asset_combined_next_interval_seconds"] = wait_next
        cfg["interval_seconds"] = wait_next
        cfg["asset_loop_prefix"] = "S"
        cfg["asset_loop_num_width"] = 3
        cfg["download_output_dir"] = str(ui_cfg.get("download_output_dir") or "").strip()
        cfg["typing_speed_profile"] = self._typing_speed_profile(ui_cfg)

        if mode == "prompt":
            cfg["asset_loop_enabled"] = False
            cfg["prompt_manual_selection"] = ""
            cfg["prompt_manual_selection_enabled"] = False
        else:
            cfg["asset_loop_enabled"] = True
            cfg["asset_use_prompt_slot"] = True
            cfg["asset_prompt_file"] = str(temp_prompt_path.resolve())
            cfg["asset_prompt_media_mode"] = "video"
            selection_spec = self._asset_selection_spec(plan_items, ui_cfg)
            cfg["asset_manual_selection"] = selection_spec
            numbers = [int(item.number) for item in plan_items if int(item.number) > 0] or self._numbers_from_cfg(ui_cfg)
            if numbers:
                cfg["asset_loop_start"] = min(numbers)
                cfg["asset_loop_end"] = max(numbers)

        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _build_runtime_prompt_text(self, *, mode: str, items: list[PromptBlock], fallback_path: Path) -> str:
        if not items:
            if fallback_path.exists():
                return fallback_path.read_text(encoding="utf-8")
            return ""
        parts: list[str] = []
        for item in items:
            if mode == "asset":
                start_tag = item.frame_start_tag or self._to_s_tag(item.number)
                end_tag = item.frame_end_tag or ""
                if end_tag:
                    parts.append(f"{start_tag}>{end_tag} Prompt : {item.rendered_prompt}")
                else:
                    parts.append(f"{start_tag} Prompt : {item.rendered_prompt}")
            else:
                parts.append(item.rendered_prompt)
        return " |||\n".join(parts)

    def _selected_project(self, ui_cfg: dict[str, Any]) -> dict[str, Any]:
        profiles = list(ui_cfg.get("project_profiles") or [])
        idx = max(0, min(int(ui_cfg.get("project_index", 0) or 0), len(profiles) - 1)) if profiles else 0
        item = profiles[idx] if profiles else {"name": "기본 프로젝트", "url": ui_cfg.get("flow_site_url", "")}
        return {"name": str(item.get("name") or item.get("project_name") or "기본 프로젝트"), "url": str(item.get("url") or "").strip()}

    def _selected_prompt_slot(self, ui_cfg: dict[str, Any]) -> dict[str, Any]:
        slots = list(ui_cfg.get("prompt_slots") or [])
        idx = max(0, min(int(ui_cfg.get("prompt_slot_index", 0) or 0), len(slots) - 1)) if slots else 0
        item = slots[idx] if slots else {"name": "기본 프롬프트 파일", "file": "prompts/flow_prompts_slot_1.txt"}
        return {"name": str(item.get("name") or "기본 프롬프트 파일"), "file": str(item.get("file") or "")}

    def _asset_selection_spec(self, plan_items: list[PromptBlock], ui_cfg: dict[str, Any]) -> str:
        numbers = [int(item.number) for item in plan_items if int(item.number) > 0]
        if numbers:
            return compress_numbers(numbers, prefix="S")
        return compress_numbers(self._numbers_from_cfg(ui_cfg), prefix="S")

    def _numbers_from_cfg(self, ui_cfg: dict[str, Any]) -> list[int]:
        mode = str(ui_cfg.get("number_mode") or "all").strip().lower()
        if mode == "manual":
            return self._parse_manual_numbers(str(ui_cfg.get("manual_numbers") or ""))
        if mode == "range":
            start = self._safe_int(ui_cfg.get("start_number"), default=1)
            end = self._safe_int(ui_cfg.get("end_number"), default=start)
            if start > end:
                start, end = end, start
            return list(range(start, end + 1))
        return []

    def _parse_manual_numbers(self, raw: str) -> list[int]:
        result: set[int] = set()
        for part in str(raw or "").replace(" ", "").split(","):
            token = part.strip().upper()
            if not token:
                continue
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

    def _typing_speed_profile(self, ui_cfg: dict[str, Any]) -> str:
        raw = str(ui_cfg.get("typing_speed_profile") or "").strip()
        if raw:
            return raw
        try:
            speed = float(ui_cfg.get("typing_speed", 1.0) or 1.0)
        except Exception:
            speed = 1.0
        if speed <= 0.8:
            return "x4"
        if speed <= 1.2:
            return "x5"
        if speed <= 1.6:
            return "x6"
        return "x7"

    def _to_s_tag(self, number: int) -> str:
        return f"S{str(int(number)).zfill(3)}"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return max(1, int(float(value)))
        except Exception:
            return int(default)

    def _launch_process(self, mode: str) -> None:
        script = self._legacy_flow_script()
        if not script.exists():
            raise RuntimeError(f"원본 Flow 워커 스크립트를 찾지 못했습니다: {script}")
        stdout_fp = open(self.stdout_path, "w", encoding="utf-8", errors="ignore")
        stderr_fp = open(self.stderr_path, "w", encoding="utf-8", errors="ignore")
        flow_project_root = str(script.parent.parent)
        cmd = [
            sys.executable,
            "-m",
            "flow.flow_auto_v2",
            "--config",
            str(self.config_path),
            "--worker",
            mode,
            "--worker-name",
            f"FlowWorker-{mode}",
            "--headless-ui",
            "--ipc-state",
            str(self.state_path),
            "--ipc-command",
            str(self.command_path),
        ]
        self.process = subprocess.Popen(
            cmd,
            cwd=flow_project_root,
            stdout=stdout_fp,
            stderr=stderr_fp,
        )
        self.log(f"숨은 원본 워커 실행: {'이미지 모드' if mode == 'prompt' else '비디오 모드'}")

    def _wait_until_ready(self) -> None:
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if self.process and self.process.poll() is not None:
                raise RuntimeError("숨은 원본 워커가 바로 종료되었습니다. worker_stderr 로그를 확인해주세요.")
            state = self.read_state()
            if state:
                return
            time.sleep(0.2)
        raise RuntimeError("숨은 원본 워커 상태 파일이 준비되지 않았습니다.")
