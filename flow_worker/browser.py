from __future__ import annotations

import shutil
import socket
import subprocess
import threading
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
        self._owner_thread_id: int | None = None

    def open_project(self, url: str, profile_dir: str, attach_url: str = "", window_cfg: dict | None = None) -> None:
        page = self.ensure_page(url=url, profile_dir=profile_dir, attach_url=attach_url, window_cfg=window_cfg)
        try:
            page.bring_to_front()
        except Exception:
            pass

    def ensure_page(self, *, url: str, profile_dir: str, attach_url: str = "", window_cfg: dict | None = None):
        self._release_thread_bound_handles_if_needed()
        if self.page and self.context:
            try:
                if not self.page.is_closed():
                    if url and url not in str(self.page.url or ""):
                        self.page.goto(url, wait_until="domcontentloaded")
                    return self.page
            except Exception:
                pass

        if self.playwright is None:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()
            self._owner_thread_id = threading.get_ident()

        debug_port = self._port_from_attach_url(attach_url or "http://127.0.0.1:9222")
        if self.edge_process is None or self.edge_process.poll() is not None or self.debug_port != debug_port:
            if self._is_port_open(debug_port):
                self.edge_process = None
                self.log(f"MS Edge 기존 디버그 포트 연결: {debug_port}")
            else:
                self._launch_edge_process(profile_dir=profile_dir, debug_port=debug_port)
        self.debug_port = debug_port
        self.browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
        self.context = self._pick_context(self.browser, url)
        self.page = self._pick_page(self.context, url)
        if self.page is None:
            self.page = self._wait_for_existing_page(self.context, timeout_sec=2.0)
        if self.page is None:
            self.page = self.context.new_page()
        if url and url not in str(self.page.url or ""):
            self.page.goto(url, wait_until="domcontentloaded")
        self._cleanup_tabs(self.context, self.page, target_url=url)
        apply_edge_window_bounds(self.page, window_cfg or {}, log=self.log, reason="작업봇 창 열기")
        self.log("브라우저 준비 완료")
        return self.page

    def stop(self, close_window: bool = False) -> None:
        if self._owner_thread_id is not None and self._owner_thread_id != threading.get_ident():
            self.browser = None
            self.context = None
            self.page = None
            self.playwright = None
            self._owner_thread_id = None
        else:
            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass
            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
        self._owner_thread_id = None
        if close_window and self.edge_process and self.edge_process.poll() is None:
            try:
                self.edge_process.terminate()
            except Exception:
                pass
            self.edge_process = None

    def _release_thread_bound_handles_if_needed(self) -> None:
        if self._owner_thread_id is None or self._owner_thread_id == threading.get_ident():
            return
        self.log("브라우저 자동화 스레드 변경 감지: CDP 재연결")
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
        self._owner_thread_id = None

    def _launch_edge_process(self, *, profile_dir: str, debug_port: int) -> None:
        edge_exe = self._resolve_msedge_executable()
        if not edge_exe:
            raise RuntimeError("MS Edge 실행 파일을 찾지 못했습니다.")
        profile_path = Path(profile_dir).resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        profile_arg = self._path_arg_for_windows_process(profile_path)
        cmd = [
            edge_exe,
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile_arg}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ]
        self.edge_process = subprocess.Popen(cmd, cwd=str(profile_path.parent))
        self.log("MS Edge 실행: about:blank")
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if self._is_port_open(debug_port):
                return
            time.sleep(0.25)
        raise RuntimeError("MS Edge 디버그 포트가 열리지 않았습니다.")

    @staticmethod
    def _is_port_open(debug_port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.25)
                return sock.connect_ex(("127.0.0.1", int(debug_port))) == 0
        except Exception:
            return False

    @staticmethod
    def _port_from_attach_url(raw: str) -> int:
        text = str(raw or "http://127.0.0.1:9222").strip()
        try:
            return int(text.rsplit(":", 1)[-1])
        except Exception:
            return 9222

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
        for page in pages:
            try:
                page_url = str(page.url or "")
            except Exception:
                page_url = ""
            if page_url.startswith(("http://", "https://")):
                return page
        for page in pages:
            try:
                page_url = str(page.url or "")
            except Exception:
                page_url = ""
            if page_url == "about:blank":
                return page
        return pages[0]

    @staticmethod
    def _wait_for_existing_page(context, timeout_sec: float = 2.0):
        deadline = time.time() + max(0.2, float(timeout_sec or 0.2))
        while time.time() < deadline:
            pages = [page for page in list(context.pages or []) if page and (not page.is_closed())]
            if pages:
                return pages[0]
            time.sleep(0.1)
        return None

    def _cleanup_tabs(self, context, keep_page, *, target_url: str = "") -> None:
        pages = [page for page in list(context.pages or []) if page and (not page.is_closed())]
        keep_url = ""
        try:
            keep_url = str(keep_page.url or "")
        except Exception:
            keep_url = ""
        for page in pages:
            if page == keep_page:
                continue
            try:
                page_url = str(page.url or "")
            except Exception:
                continue
            should_close = False
            if page_url == "about:blank":
                should_close = True
            elif target_url and page_url == keep_url and page_url.startswith(("http://", "https://")):
                should_close = True
            if not should_close:
                continue
            try:
                page.close()
            except Exception:
                pass

    @staticmethod
    def _resolve_msedge_executable() -> str:
        candidates = [
            shutil.which("msedge.exe"),
            shutil.which("msedge"),
            "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
            "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for item in candidates:
            if item and Path(item).exists():
                return str(item)
        return ""

    @staticmethod
    def _path_arg_for_windows_process(path: Path) -> str:
        text = str(path)
        if not text.startswith("/mnt/"):
            return text
        try:
            converted = subprocess.check_output(["wslpath", "-w", text], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            converted = ""
        return converted or text
