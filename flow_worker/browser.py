from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable

from .windowing import apply_edge_window_bounds


LogFn = Callable[[str], None]


class BrowserManager:
    def __init__(self, log: LogFn | None = None):
        self.log = log or (lambda message: None)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.edge_process = None
        self.debug_port = None

    def open_project(self, url: str, profile_dir: str, attach_url: str = "", window_cfg: dict | None = None) -> None:
        page = self.ensure_page(url=url, profile_dir=profile_dir, attach_url=attach_url, window_cfg=window_cfg)
        try:
            page.bring_to_front()
        except Exception:
            pass

    def ensure_page(self, *, url: str, profile_dir: str, attach_url: str = "", window_cfg: dict | None = None):
        if self.page and self.context:
            try:
                if not self.page.is_closed():
                    if url and url not in str(self.page.url or ""):
                        self.page.goto(url, wait_until="domcontentloaded")
                    self._activate_page_window(self.page)
                    apply_edge_window_bounds(self.page, window_cfg or {}, log=self.log, reason="기존 창 재사용")
                    return self.page
            except Exception:
                pass

        if self.playwright is None:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()

        debug_port = self._port_from_attach_url(attach_url or "http://127.0.0.1:9333")
        should_launch = False
        if self.edge_process is not None and self.edge_process.poll() is None and self.debug_port == debug_port:
            should_launch = False
        elif self._is_debug_port_open(debug_port):
            self.log(f"기존 FlowWorker Edge 포트 재사용: {debug_port}")
        else:
            should_launch = True
        if should_launch:
            self._launch_edge_process(profile_dir=profile_dir, url=url, debug_port=debug_port)
        self.debug_port = debug_port
        self.browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
        self.context = self._pick_context(self.browser, url)
        self.page = self._pick_page(self.context, url)
        if self.page is None:
            self.page = self.context.new_page()
        self._close_duplicate_tabs(self.context, keep_page=self.page, target_url=url)
        if url and url not in str(self.page.url or ""):
            self.page.goto(url, wait_until="domcontentloaded")
        self._close_duplicate_tabs(self.context, keep_page=self.page, target_url=url)
        self._activate_page_window(self.page)
        apply_edge_window_bounds(self.page, window_cfg or {}, log=self.log, reason="작업봇 창 열기")
        self.log("브라우저 준비 완료")
        return self.page

    def stop(self, close_window: bool = False) -> None:
        self.browser = None
        self.context = None
        self.page = None
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.playwright = None
        if close_window and self.edge_process and self.edge_process.poll() is None:
            try:
                self.edge_process.terminate()
            except Exception:
                pass
            self.edge_process = None

    def _launch_edge_process(self, *, profile_dir: str, url: str, debug_port: int) -> None:
        edge_exe = self._resolve_msedge_executable()
        if not edge_exe:
            raise RuntimeError("MS Edge 실행 파일을 찾지 못했습니다.")
        profile_path = Path(profile_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        cmd = [
            edge_exe,
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={str(profile_path)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ]
        self.edge_process = subprocess.Popen(cmd, cwd=str(profile_path.parent))
        self.log(f"MS Edge 실행: {url or 'about:blank'}")
        deadline = time.time() + 15.0
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                if sock.connect_ex(("127.0.0.1", debug_port)) == 0:
                    return
            time.sleep(0.25)
        raise RuntimeError("MS Edge 디버그 포트가 열리지 않았습니다.")

    @staticmethod
    def _is_debug_port_open(debug_port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", int(debug_port))) == 0

    def _activate_page_window(self, page) -> None:
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            session = page.context.new_cdp_session(page)
            info = session.send("Browser.getWindowForTarget")
            window_id = int(info.get("windowId") or 0)
            if window_id > 0:
                session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
        except Exception:
            pass

    def _close_duplicate_tabs(self, context, *, keep_page, target_url: str = "") -> None:
        pages = [page for page in list(context.pages or []) if page and (not page.is_closed())]
        target_url = str(target_url or "").strip()
        target_pages = []
        blank_pages = []
        for page in pages:
            try:
                current_url = str(page.url or "").strip()
            except Exception:
                current_url = ""
            if target_url and target_url in current_url:
                target_pages.append(page)
            elif current_url == "about:blank":
                blank_pages.append(page)
        if target_pages:
            target_keep = target_pages[0]
            if keep_page in target_pages:
                target_keep = keep_page
            self.page = target_keep
            for page in pages:
                if page is target_keep:
                    continue
                try:
                    page.close()
                except Exception:
                    pass
            return
        for page in pages:
            if page is keep_page:
                continue
            try:
                current_url = str(page.url or "").strip()
            except Exception:
                current_url = ""
            if current_url == "about:blank" and keep_page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    @staticmethod
    def _port_from_attach_url(raw: str) -> int:
        text = str(raw or "http://127.0.0.1:9333").strip()
        try:
            return int(text.rsplit(":", 1)[-1])
        except Exception:
            return 9333

    @staticmethod
    def _pick_context(browser, target_url: str):
        contexts = list(browser.contexts or [])
        if not contexts:
            raise RuntimeError("Edge 컨텍스트를 찾지 못했습니다.")
        for context in contexts:
            for page in list(context.pages or []):
                try:
                    if target_url and target_url in str(page.url or ""):
                        return context
                except Exception:
                    continue
        return contexts[0]

    @staticmethod
    def _pick_page(context, target_url: str):
        pages = [page for page in list(context.pages or []) if page and (not page.is_closed())]
        if not pages:
            return None
        for page in pages:
            try:
                if target_url and target_url in str(page.url or ""):
                    return page
            except Exception:
                continue
        return pages[0]

    @staticmethod
    def _resolve_msedge_executable() -> str:
        candidates = [
            shutil.which("msedge.exe"),
            shutil.which("msedge"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for item in candidates:
            if item and Path(item).exists():
                return str(item)
        return ""
