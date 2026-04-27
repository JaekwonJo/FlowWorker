from __future__ import annotations

import time
from typing import Callable


LogFn = Callable[[str], None]


def _setting(cfg: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(cfg.get(key, default) or default)
    except Exception:
        value = default
    return max(low, min(high, value))


def edge_window_settings(cfg: dict) -> dict[str, int]:
    return {
        "inner_width": _setting(cfg, "edge_window_inner_width", 968, 760, 2200),
        "inner_height": _setting(cfg, "edge_window_inner_height", 940, 700, 1800),
        "left": _setting(cfg, "edge_window_left", 0, -3000, 6000),
        "top": _setting(cfg, "edge_window_top", 0, -2000, 4000),
        "lock_position": bool(cfg.get("edge_window_lock_position", False)),
    }


def _read_metrics(page) -> dict[str, int]:
    metrics = page.evaluate(
        """
        () => ({
            innerWidth: Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0),
            innerHeight: Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0),
            outerWidth: Math.max(window.outerWidth || 0, 0),
            outerHeight: Math.max(window.outerHeight || 0, 0),
            screenX: Math.round(window.screenX || 0),
            screenY: Math.round(window.screenY || 0),
            availWidth: Math.max((window.screen && window.screen.availWidth) || 0, 0),
            availHeight: Math.max((window.screen && window.screen.availHeight) || 0, 0),
        })
        """
    ) or {}
    return {key: int(metrics.get(key) or 0) for key in ("innerWidth", "innerHeight", "outerWidth", "outerHeight", "screenX", "screenY", "availWidth", "availHeight")}


def apply_edge_window_bounds(page, cfg: dict, *, log: LogFn | None = None, reason: str = "") -> bool:
    settings = edge_window_settings(cfg)
    note = f" ({reason})" if reason else ""
    try:
        metrics = _read_metrics(page)
        border_w = max(0, metrics["outerWidth"] - metrics["innerWidth"])
        border_h = max(0, metrics["outerHeight"] - metrics["innerHeight"])
        outer_w = settings["inner_width"] + border_w
        outer_h = settings["inner_height"] + border_h
        left = settings["left"] if settings["lock_position"] else metrics["screenX"]
        top = settings["top"] if settings["lock_position"] else metrics["screenY"]

        session = page.context.new_cdp_session(page)
        info = session.send("Browser.getWindowForTarget")
        window_id = int(info.get("windowId") or 0)
        if window_id <= 0:
            raise RuntimeError("windowId를 찾지 못했습니다.")

        bounds = {"windowState": "normal", "width": int(outer_w), "height": int(outer_h)}
        if settings["lock_position"]:
            bounds["left"] = int(left)
            bounds["top"] = int(top)
        session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
        time.sleep(0.2)
        if log:
            corrected = _read_metrics(page)
            log(f"🪟 Edge 창 크기 맞춤{note}: 내부 {corrected['innerWidth']}x{corrected['innerHeight']} | 위치 {corrected['screenX']},{corrected['screenY']}")
        return True
    except Exception as exc:
        if log:
            log(f"⚠️ Edge 창 크기 맞춤 실패{note}: {exc}")
        return False
