from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import CONFIG_FILE, DEFAULT_CONFIG, load_config, save_config


def _slot_dir(base_dir: Path) -> Path:
    path = base_dir / "runtime" / "worker_slots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slot_path(base_dir: Path, index: int) -> Path:
    return _slot_dir(base_dir) / f"worker_slot_{int(index)}.json"


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def _prune_stale_slots(base_dir: Path) -> set[int]:
    active: set[int] = set()
    for path in _slot_dir(base_dir).glob("worker_slot_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        try:
            idx = int(payload.get("worker_index") or 0)
        except Exception:
            idx = 0
        try:
            pid = int(payload.get("pid") or 0)
        except Exception:
            pid = 0
        if idx > 0 and _is_pid_alive(pid):
            active.add(idx)
            continue
        try:
            path.unlink()
        except Exception:
            pass
    return active


def _next_free_index(active: set[int]) -> int:
    idx = 1
    while idx in active:
        idx += 1
    return idx


def _config_name_for_index(index: int) -> str:
    return CONFIG_FILE if int(index) == 1 else f"flow_worker_config_worker{int(index)}.json"


def _port_for_index(index: int) -> str:
    return f"http://127.0.0.1:{9332 + int(index)}"


def _profile_name_for_index(index: int) -> str:
    return f"flowworker_profile_{int(index)}"


def _profile_dir_for_index(index: int) -> str:
    return f"runtime/flow_worker_edge_profile_{int(index)}"


def _prepare_instance_config(base_dir: Path, index: int) -> str:
    config_name = _config_name_for_index(index)
    if config_name == CONFIG_FILE:
        cfg = load_config(base_dir, config_name=config_name)
    else:
        path = base_dir / config_name
        if path.exists():
            cfg = load_config(base_dir, config_name=config_name)
        else:
            seed = load_config(base_dir, config_name=CONFIG_FILE)
            cfg = dict(seed)
    cfg["worker_index"] = int(index)
    cfg["worker_name"] = f"Flow Worker{int(index)}"
    cfg["browser_profile_name"] = _profile_name_for_index(index)
    cfg["browser_profile_dir"] = _profile_dir_for_index(index)
    cfg["browser_attach_url"] = _port_for_index(index)
    save_config(base_dir, cfg, config_name=config_name)
    return config_name


def launch_next_worker(base_dir: Path, *, index: int | None = None) -> int:
    active = _prune_stale_slots(base_dir)
    worker_index = int(index) if index and int(index) > 0 else _next_free_index(active)
    config_name = _prepare_instance_config(base_dir, worker_index)
    slot_path = _slot_path(base_dir, worker_index)
    cmd = [
        sys.executable,
        "-m",
        "flow_worker.main",
        "--config-name",
        config_name,
        "--slot-file",
        str(slot_path),
    ]
    proc = subprocess.Popen(cmd, cwd=str(base_dir))
    return proc.pid


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    base_dir = Path(__file__).resolve().parent.parent
    launch_next_worker(base_dir, index=args.index or None)


if __name__ == "__main__":
    main()
