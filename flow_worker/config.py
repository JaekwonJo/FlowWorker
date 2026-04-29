from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any


CONFIG_FILE = "flow_worker_config.json"
PROMPTS_DIR = "prompts"
RUNTIME_DIR = "runtime"
LOGS_DIR = "logs"
DOWNLOADS_DIR = "downloads"
DEFAULT_BROWSER_PROFILE_DIR = f"{RUNTIME_DIR}/flow_worker_edge_profile"
DEFAULT_BROWSER_ATTACH_URL = "http://127.0.0.1:9333"


DEFAULT_CONFIG: dict[str, Any] = {
    "worker_name": "Flow Worker1",
    "project_profiles": [
        {
            "name": "기본 프로젝트",
            "url": "https://labs.google/fx/ko/tools/flow",
        }
    ],
    "project_index": 0,
    "prompt_slots": [
        {
            "name": "기본 프롬프트 파일",
            "file": f"{PROMPTS_DIR}/flow_prompts_slot_1.txt",
        }
    ],
    "prompt_slot_index": 0,
    "prompt_separator": "|||",
    "prompt_prefix": "S",
    "prompt_pad_width": 3,
    "number_mode": "all",
    "start_number": 1,
    "end_number": 10,
    "manual_numbers": "",
    "download_output_dir": "",
    "browser_profile_dir": DEFAULT_BROWSER_PROFILE_DIR,
    "browser_attach_url": DEFAULT_BROWSER_ATTACH_URL,
    "edge_window_inner_width": 968,
    "edge_window_inner_height": 940,
    "edge_window_left": 0,
    "edge_window_top": 0,
    "edge_window_lock_position": False,
    "media_mode": "image",
    "image_variant_count": "x1",
    "video_variant_count": "x1",
    "image_quality": "1K",
    "video_quality": "1080P",
    "generate_wait_seconds": 10.0,
    "next_prompt_wait_seconds": 7.0,
    "window_geometry": "1060x760",
    "settings_collapsed": False,
    "log_panel_visible": False,
    "flow_site_url": "https://labs.google/fx/ko/tools/flow",
}


def _merge_defaults(defaults: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in (data or {}).items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_app_dirs(base_dir: Path) -> None:
    (base_dir / PROMPTS_DIR).mkdir(parents=True, exist_ok=True)
    (base_dir / RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    (base_dir / LOGS_DIR).mkdir(parents=True, exist_ok=True)
    (base_dir / DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)


def config_path(base_dir: Path, config_name: str = CONFIG_FILE) -> Path:
    return base_dir / (str(config_name or CONFIG_FILE).strip() or CONFIG_FILE)


def _legacy_flow_root(base_dir: Path) -> Path:
    return base_dir.parent / "Flow Classic Plus" / "flow"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _copy_legacy_prompt_files(base_dir: Path, legacy_root: Path, prompt_slots: list[dict[str, Any]]) -> list[dict[str, str]]:
    copied: list[dict[str, str]] = []
    for idx, slot in enumerate(prompt_slots or [], start=1):
        name = str((slot or {}).get("name") or f"프롬프트 파일 {idx}").strip() or f"프롬프트 파일 {idx}"
        file_name = str((slot or {}).get("file") or "").strip()
        if not file_name:
            continue
        source = legacy_root / file_name
        rel = f"{PROMPTS_DIR}/flow_prompts_slot_{idx}.txt"
        target = base_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            try:
                shutil.copyfile(source, target)
            except Exception:
                target.write_text(source.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
        else:
            target.write_text("", encoding="utf-8")
        copied.append({"name": name, "file": rel})
    return copied


def bootstrap_from_legacy_flow(base_dir: Path) -> dict[str, Any] | None:
    legacy_root = _legacy_flow_root(base_dir)
    legacy_cfg_path = legacy_root / "flow_config.json"
    if not legacy_cfg_path.exists():
        return None
    legacy_cfg = _read_json(legacy_cfg_path)
    if not legacy_cfg:
        return None
    cfg = deepcopy(DEFAULT_CONFIG)
    profiles = list(legacy_cfg.get("project_profiles") or [])
    mapped_profiles = []
    for item in profiles:
        mapped_profiles.append(
            {
                "name": str((item or {}).get("project_name") or (item or {}).get("name") or "프로젝트").strip() or "프로젝트",
                "url": str((item or {}).get("url") or cfg["flow_site_url"]).strip() or cfg["flow_site_url"],
            }
        )
    if mapped_profiles:
        cfg["project_profiles"] = mapped_profiles
        cfg["project_index"] = max(0, min(int(legacy_cfg.get("active_project_profile", 0) or 0), len(mapped_profiles) - 1))
    copied_slots = _copy_legacy_prompt_files(base_dir, legacy_root, list(legacy_cfg.get("prompt_slots") or []))
    if copied_slots:
        cfg["prompt_slots"] = copied_slots
        cfg["prompt_slot_index"] = max(0, min(int(legacy_cfg.get("active_prompt_slot", 0) or 0), len(copied_slots) - 1))
    return cfg


def _ensure_prompt_slots(base_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    prompt_slots = list(cfg.get("prompt_slots") or [])
    if not prompt_slots:
        prompt_slots = deepcopy(DEFAULT_CONFIG["prompt_slots"])
    normalized_slots: list[dict[str, str]] = []
    for idx, slot in enumerate(prompt_slots, start=1):
        slot_name = str((slot or {}).get("name") or f"프롬프트 파일 {idx}").strip() or f"프롬프트 파일 {idx}"
        slot_file = str((slot or {}).get("file") or f"{PROMPTS_DIR}/flow_prompts_slot_{idx}.txt").strip() or f"{PROMPTS_DIR}/flow_prompts_slot_{idx}.txt"
        normalized_slots.append({"name": slot_name, "file": slot_file})
        path = base_dir / slot_file
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
    cfg["prompt_slots"] = normalized_slots
    if not cfg.get("download_output_dir"):
        cfg["download_output_dir"] = str((base_dir / DOWNLOADS_DIR).resolve())
    project_profiles = list(cfg.get("project_profiles") or [])
    if not project_profiles:
        cfg["project_profiles"] = deepcopy(DEFAULT_CONFIG["project_profiles"])
        project_profiles = list(cfg["project_profiles"])
    profile_dir = str(cfg.get("browser_profile_dir") or "").strip().replace("\\", "/")
    attach_url = str(cfg.get("browser_attach_url") or "").strip()
    if not profile_dir or profile_dir in {f"{RUNTIME_DIR}/edge_profile_1", "runtime/edge_profile_1"}:
        cfg["browser_profile_dir"] = DEFAULT_BROWSER_PROFILE_DIR
    if not attach_url or attach_url in {"http://127.0.0.1:9222", "127.0.0.1:9222"}:
        cfg["browser_attach_url"] = DEFAULT_BROWSER_ATTACH_URL
    (base_dir / str(cfg.get("browser_profile_dir") or DEFAULT_BROWSER_PROFILE_DIR)).mkdir(parents=True, exist_ok=True)
    cfg["project_index"] = max(0, min(int(cfg.get("project_index", 0) or 0), len(project_profiles) - 1))
    cfg["prompt_slot_index"] = max(0, min(int(cfg.get("prompt_slot_index", 0) or 0), len(normalized_slots) - 1))
    return cfg


def load_config(base_dir: Path, config_name: str = CONFIG_FILE) -> dict[str, Any]:
    ensure_app_dirs(base_dir)
    path = config_path(base_dir, config_name)
    if not path.exists():
        cfg = bootstrap_from_legacy_flow(base_dir) or deepcopy(DEFAULT_CONFIG)
        cfg = _ensure_prompt_slots(base_dir, cfg)
        save_config(base_dir, cfg, config_name)
        return cfg
    cfg = _merge_defaults(DEFAULT_CONFIG, _read_json(path))
    return _ensure_prompt_slots(base_dir, cfg)


def save_config(base_dir: Path, cfg: dict[str, Any], config_name: str = CONFIG_FILE) -> Path:
    ensure_app_dirs(base_dir)
    normalized = _ensure_prompt_slots(base_dir, _merge_defaults(DEFAULT_CONFIG, cfg))
    path = config_path(base_dir, config_name)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def next_prompt_slot_file(base_dir: Path, existing_slots: list[dict[str, Any]]) -> str:
    used = {str((slot or {}).get("file") or "").strip() for slot in (existing_slots or [])}
    idx = 1
    while True:
        rel = f"{PROMPTS_DIR}/flow_prompts_slot_{idx}.txt"
        if rel not in used and not (base_dir / rel).exists():
            return rel
        idx += 1
