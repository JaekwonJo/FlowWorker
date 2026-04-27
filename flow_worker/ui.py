from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

from .automation import FlowAutomationEngine
from .browser import BrowserManager
from .config import CONFIG_FILE, load_config, next_prompt_slot_file, save_config
from .prompt_parser import compress_numbers, summarize_prompt_file
from .queue_state import QueueItem


def _open_path(path: Path) -> None:
    try:
        os.startfile(str(path))
        return
    except Exception:
        pass
    subprocess.Popen(["cmd.exe", "/c", "start", "", str(path)])


class FlowWorkerApp:
    def __init__(self) -> None:
        self.base_dir = Path(__file__).resolve().parent.parent
        self.cfg = load_config(self.base_dir, config_name=CONFIG_FILE)
        self.browser = BrowserManager(self.log)
        self.queue_items: list[QueueItem] = []
        self.log_lines: list[str] = []
        self.run_thread: threading.Thread | None = None
        self.stop_requested = False
        self.paused = False
        self.settings_collapsed = bool(self.cfg.get("settings_collapsed", False))
        self.log_panel_visible = bool(self.cfg.get("log_panel_visible", False))
        self._resize_drag_origin: tuple[int, int, int, int] | None = None
        self._suspend_auto_save = True

        self.root = tk.Tk()
        self.root.title(f"Flow Worker - {self.cfg.get('worker_name', 'Flow Worker1')}")
        self.root.geometry(str(self.cfg.get("window_geometry") or "1060x760"))
        self.root.minsize(900, 560)
        self.root.configure(bg=self._bg("root_bg"))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._apply_window_icon()

        self._build_vars()
        self._build_ui()
        self._load_vars_from_config()
        self._apply_settings_visibility()
        self._apply_log_panel_visibility()
        self._apply_media_visibility()
        self._suspend_auto_save = False
        self.refresh_all()

    def _build_vars(self) -> None:
        self.worker_name_var = tk.StringVar()
        self.project_var = tk.StringVar()
        self.prompt_slot_var = tk.StringVar()
        self.download_dir_var = tk.StringVar()
        self.media_mode_var = tk.StringVar()
        self.number_mode_var = tk.StringVar()
        self.start_number_var = tk.StringVar()
        self.end_number_var = tk.StringVar()
        self.manual_numbers_var = tk.StringVar()
        self.image_variant_var = tk.StringVar()
        self.video_variant_var = tk.StringVar()
        self.image_quality_var = tk.StringVar()
        self.video_quality_var = tk.StringVar()
        self.typing_speed_var = tk.DoubleVar()
        self.humanize_typing_var = tk.BooleanVar()
        self.generate_wait_var = tk.StringVar()
        self.next_wait_var = tk.StringVar()
        self.status_var = tk.StringVar(value="준비 완료")
        self.progress_var = tk.StringVar(value="0 / 0 (0.0%)")
        self.project_summary_var = tk.StringVar(value="사이트: flow")
        self.queue_summary_var = tk.StringVar(value="활성 0개 | 완료 0 | 실패 0 | 대기 0")
        self.prompt_file_summary_var = tk.StringVar(value="")
        self.attach_url_var = tk.StringVar(value="")

    def _bg(self, key: str) -> str:
        theme = {
            "root_bg": "#181511",
            "top_left_bg": "#2C2019",
            "top_left_border": "#B87A3E",
            "top_mid_bg": "#31251D",
            "top_mid_border": "#D79248",
            "top_right_bg": "#2C2019",
            "settings_bg": "#2A241E",
            "settings_border": "#C58A4B",
            "queue_panel_bg": "#221C17",
            "queue_panel_border": "#B87A3E",
            "log_panel_bg": "#161311",
            "log_text_bg": "#100E0C",
            "log_text_fg": "#F7EBDD",
            "muted_fg": "#D9C1A6",
            "sub_fg": "#E8D5BF",
            "chip_bg": "#3A2B21",
            "chip_fg": "#F4C27E",
            "progress_bg": "#1A1612",
            "progress_border": "#6F4A28",
            "progress_fill": "#E3A054",
            "small_btn_bg": "#4A3526",
            "open_btn_bg": "#7D522E",
            "start_btn_bg": "#2E7A54",
            "settings_toggle_bg": "#5D442D",
            "status_fg": "#8FF0A8",
        }
        return theme[key]

    def _apply_window_icon(self) -> None:
        icon_path = self.base_dir / "flow_worker" / "assets" / "flow_worker_icon.png"
        if not icon_path.exists():
            return
        try:
            self.window_icon = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self.window_icon)
        except Exception:
            self.window_icon = None

    def _build_ui(self) -> None:
        root = self.root

        top = tk.Frame(root, bg=self._bg("root_bg"))
        top.pack(fill="x", padx=10, pady=(10, 6))

        top_left = tk.Frame(top, bg=self._bg("top_left_bg"), highlightbackground=self._bg("top_left_border"), highlightthickness=1)
        top_left.pack(side="left", fill="both", expand=True)
        tk.Label(top_left, text="Flow Worker", bg=self._bg("top_left_bg"), fg="#FFFFFF", font=("Malgun Gothic", 13, "bold")).pack(anchor="w", padx=10, pady=(6, 1))
        tk.Label(top_left, textvariable=self.worker_name_var, bg=self._bg("top_left_bg"), fg=self._bg("muted_fg"), font=("Malgun Gothic", 9)).pack(anchor="w", padx=10, pady=(0, 6))

        top_mid = tk.Frame(top, bg=self._bg("top_mid_bg"), width=250, highlightbackground=self._bg("top_mid_border"), highlightthickness=1)
        top_mid.pack(side="left", padx=8, fill="y")
        tk.Label(top_mid, text="진행 상황", bg=self._bg("top_mid_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10, "bold")).pack(pady=(6, 1))
        tk.Label(top_mid, textvariable=self.project_summary_var, bg=self._bg("top_mid_bg"), fg=self._bg("sub_fg"), font=("Malgun Gothic", 8)).pack()
        tk.Label(top_mid, textvariable=self.progress_var, bg=self._bg("top_mid_bg"), fg=self._bg("chip_fg"), font=("Consolas", 12, "bold")).pack(pady=(2, 3))
        self.progress_canvas = tk.Canvas(top_mid, width=230, height=14, bg=self._bg("progress_bg"), highlightthickness=1, highlightbackground=self._bg("progress_border"))
        self.progress_canvas.pack(padx=10, pady=(0, 6))
        self.progress_fill = self.progress_canvas.create_rectangle(0, 0, 0, 18, fill=self._bg("progress_fill"), outline="")

        top_right = tk.Frame(top, bg=self._bg("top_right_bg"), highlightbackground=self._bg("top_left_border"), highlightthickness=1)
        top_right.pack(side="left", fill="both", expand=True)
        tk.Label(top_right, text="현재 상태", bg=self._bg("top_right_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10, "bold")).pack(anchor="e", padx=10, pady=(6, 1))
        tk.Label(top_right, textvariable=self.status_var, bg=self._bg("top_right_bg"), fg=self._bg("status_fg"), font=("Malgun Gothic", 10, "bold"), wraplength=180, justify="right").pack(anchor="e", padx=10, pady=(0, 6))

        action_row = tk.Frame(root, bg=self._bg("root_bg"))
        action_row.pack(fill="x", padx=10, pady=(0, 6))
        action_left = tk.Frame(action_row, bg=self._bg("root_bg"))
        action_left.pack(side="left")
        action_right = tk.Frame(action_row, bg=self._bg("root_bg"))
        action_right.pack(side="right")

        self._action_button(action_left, "완전정지", self.stop_all, self._bg("small_btn_bg"), small=True).pack(side="left", padx=(0, 6))
        self._action_button(action_left, "일시정지", self.pause_run, self._bg("small_btn_bg"), small=True).pack(side="left", padx=6)
        self._action_button(action_left, "재개", self.resume_run, self._bg("small_btn_bg"), small=True).pack(side="left", padx=6)
        self.settings_toggle_btn = self._action_button(action_left, "⚙ 설정 접기", self.toggle_settings_panel, self._bg("settings_toggle_bg"), small=True)
        self.settings_toggle_btn.pack(side="left", padx=6)
        self._action_button(action_right, "작업봇 창 열기", self.open_browser_window, self._bg("open_btn_bg")).pack(side="left", padx=(0, 6))
        self.start_btn = self._action_button(action_right, "▶ 시작", self.start_run, self._bg("start_btn_bg"))
        self.start_btn.pack(side="left")

        body = tk.Frame(root, bg=self._bg("root_bg"))
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        settings = tk.Frame(body, bg=self._bg("settings_bg"), highlightbackground=self._bg("settings_border"), highlightthickness=1)
        self.settings_frame = settings
        settings.pack(fill="x")
        settings.grid_columnconfigure(0, weight=6)
        settings.grid_columnconfigure(1, weight=4)

        left = tk.Frame(settings, bg=self._bg("settings_bg"))
        left.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=8)
        right = tk.Frame(settings, bg=self._bg("settings_bg"))
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=8)

        self._build_basic_settings(left)
        self._build_number_settings(right)

        lower = tk.Frame(body, bg=self._bg("root_bg"))
        self.lower_frame = lower
        lower.pack(fill="both", expand=True, pady=(8, 0))
        lower_content = tk.Frame(lower, bg=self._bg("root_bg"))
        self.lower_content = lower_content
        lower_content.pack(fill="both", expand=True)

        queue_wrap = tk.Frame(lower_content, bg=self._bg("queue_panel_bg"), highlightbackground=self._bg("queue_panel_border"), highlightthickness=1)
        self.queue_wrap = queue_wrap
        queue_wrap.pack(side="left", fill="both", expand=True)

        queue_header = tk.Frame(queue_wrap, bg=self._bg("queue_panel_bg"))
        queue_header.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(queue_header, text="대기열", bg=self._bg("queue_panel_bg"), fg="#FFFFFF", font=("Malgun Gothic", 10, "bold")).pack(side="left")
        self.log_toggle_btn = self._action_button(queue_header, "로그 보기", self.toggle_log_panel, self._bg("open_btn_bg"), small=True)
        self.log_toggle_btn.pack(side="right", padx=(8, 0))
        self._action_button(queue_header, "실패 번호 복붙", self.copy_failed_numbers, self._bg("open_btn_bg"), small=True).pack(side="right", padx=(8, 0))
        self._action_button(queue_header, "번호복사", self.copy_prompt_numbers, self._bg("open_btn_bg"), small=True).pack(side="right", padx=(8, 0))
        tk.Label(queue_header, textvariable=self.queue_summary_var, bg=self._bg("queue_panel_bg"), fg=self._bg("sub_fg"), font=("Malgun Gothic", 8)).pack(side="right", padx=(10, 12))

        queue_body = tk.Frame(queue_wrap, bg=self._bg("queue_panel_bg"))
        queue_body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.queue_canvas = tk.Canvas(queue_body, bg=self._bg("queue_panel_bg"), highlightthickness=0)
        self.queue_scroll = tk.Scrollbar(queue_body, orient="vertical", command=self.queue_canvas.yview)
        self.queue_canvas.configure(yscrollcommand=self.queue_scroll.set)
        self.queue_scroll.pack(side="right", fill="y")
        self.queue_canvas.pack(side="left", fill="both", expand=True)
        self.queue_inner = tk.Frame(self.queue_canvas, bg=self._bg("queue_panel_bg"))
        self.queue_window = self.queue_canvas.create_window((0, 0), window=self.queue_inner, anchor="nw")
        self.queue_inner.bind("<Configure>", lambda _e: self._update_queue_scroll())
        self.queue_canvas.bind("<Configure>", self._on_queue_canvas_resize)
        self.queue_canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        log_frame = tk.Frame(lower_content, bg=self._bg("log_panel_bg"), highlightbackground=self._bg("queue_panel_border"), highlightthickness=1, width=250)
        self.log_frame = log_frame
        log_frame.pack_propagate(False)
        tk.Label(log_frame, text="로그", bg=self._bg("log_panel_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10, "bold")).pack(anchor="w", padx=8, pady=(8, 4))
        self.log_text = tk.Text(log_frame, height=6, bg=self._bg("log_text_bg"), fg=self._bg("log_text_fg"), insertbackground="#FFFFFF", relief="solid", borderwidth=1)
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text.configure(state="disabled")

        resize_handle = tk.Frame(root, bg=self._bg("small_btn_bg"), width=36, height=22, cursor="size_nw_se", highlightbackground=self._bg("top_left_border"), highlightthickness=1)
        resize_handle.place(relx=1.0, rely=1.0, x=-8, y=-8, anchor="se")
        resize_handle.pack_propagate(False)
        tk.Label(resize_handle, text="◢", bg=self._bg("small_btn_bg"), fg="#FFFFFF", font=("Malgun Gothic", 10, "bold")).pack(expand=True)
        resize_handle.bind("<ButtonPress-1>", self._start_resize_drag)
        resize_handle.bind("<B1-Motion>", self._on_resize_drag)
        resize_handle.bind("<ButtonRelease-1>", self._end_resize_drag)

    def _build_basic_settings(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="기본 설정", bg=self._bg("settings_bg"), fg="#FFFFFF", font=("Malgun Gothic", 11, "bold")).pack(anchor="w", padx=4, pady=(0, 6))

        self._labeled_combo(parent, "프로젝트", self.project_var, self.project_changed)
        project_btns = tk.Frame(parent, bg=self._bg("settings_bg"))
        project_btns.pack(fill="x", padx=4, pady=(0, 4))
        self._action_button(project_btns, "프로젝트 추가", self.add_project, self._bg("open_btn_bg"), small=True, width=9).pack(side="left", padx=(0, 6))
        self._action_button(project_btns, "이름수정", self.rename_project, self._bg("open_btn_bg"), small=True, width=8).pack(side="left", padx=6)
        self._action_button(project_btns, "URL 편집", self.edit_project_url, self._bg("open_btn_bg"), small=True, width=8).pack(side="left", padx=6)
        self._action_button(project_btns, "삭제", self.delete_project, self._bg("open_btn_bg"), small=True, width=6).pack(side="left", padx=(6, 0))

        self._labeled_combo(parent, "프롬프트 파일", self.prompt_slot_var, self.prompt_slot_changed)
        prompt_btns = tk.Frame(parent, bg=self._bg("settings_bg"))
        prompt_btns.pack(fill="x", padx=4, pady=(0, 4))
        self._action_button(prompt_btns, "파일 열기", self.open_prompt_file, self._bg("open_btn_bg"), small=True, width=8).pack(side="left", padx=(0, 6))
        self._action_button(prompt_btns, "이름수정", self.rename_prompt_file, self._bg("open_btn_bg"), small=True, width=8).pack(side="left", padx=6)
        self._action_button(prompt_btns, "삭제", self.delete_prompt_file, self._bg("open_btn_bg"), small=True, width=6).pack(side="left", padx=6)
        self._action_button(prompt_btns, "추가", self.add_prompt_file, self._bg("open_btn_bg"), small=True, width=6).pack(side="left", padx=(6, 0))
        prompt_summary_row = tk.Frame(parent, bg=self._bg("settings_bg"))
        prompt_summary_row.pack(fill="x", padx=4, pady=(0, 8))
        tk.Label(prompt_summary_row, textvariable=self.prompt_file_summary_var, bg=self._bg("settings_bg"), fg=self._bg("sub_fg"), font=("Malgun Gothic", 9), justify="left", anchor="nw", height=3).pack(side="left", fill="x", expand=True, anchor="w")

        self._path_row(parent, "저장 폴더", self.download_dir_var, self.choose_download_dir)
        attach_note = tk.Frame(parent, bg=self._bg("settings_bg"))
        attach_note.pack(fill="x", padx=4, pady=(0, 8))
        tk.Label(attach_note, text="브라우저 연결", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(anchor="w")
        tk.Label(attach_note, textvariable=self.attach_url_var, bg=self._bg("chip_bg"), fg=self._bg("chip_fg"), font=("Consolas", 10, "bold"), padx=10, pady=5).pack(anchor="w", pady=(6, 0))

    def _build_number_settings(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="번호 설정", bg=self._bg("settings_bg"), fg="#FFFFFF", font=("Malgun Gothic", 11, "bold")).pack(anchor="w", padx=4, pady=(0, 6))

        media_mode_row = tk.Frame(parent, bg=self._bg("settings_bg"))
        media_mode_row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(media_mode_row, text="작업 모드", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        for text, value in (("이미지", "image"), ("비디오", "video")):
            tk.Radiobutton(media_mode_row, text=text, value=value, variable=self.media_mode_var, command=self.on_media_mode_changed, bg=self._bg("settings_bg"), fg="#FFFFFF", selectcolor=self._bg("chip_bg"), activebackground=self._bg("settings_bg"), activeforeground="#FFFFFF", font=("Malgun Gothic", 10)).pack(side="left", padx=(12, 12))

        self.image_settings_frame = tk.Frame(parent, bg=self._bg("settings_bg"))
        image_row = tk.Frame(self.image_settings_frame, bg=self._bg("settings_bg"))
        image_row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(image_row, text="생성 개수", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.image_variant_combo = self._choice_menu(image_row, self.image_variant_var, ("x1", "x2", "x3", "x4"))
        self.image_variant_combo.pack(side="left", padx=(12, 12))
        tk.Label(image_row, text="이미지 화질", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.image_quality_combo = self._choice_menu(image_row, self.image_quality_var, ("1K", "2K", "4K"))
        self.image_quality_combo.pack(side="left", padx=(12, 0))

        self.video_settings_frame = tk.Frame(parent, bg=self._bg("settings_bg"))
        video_row = tk.Frame(self.video_settings_frame, bg=self._bg("settings_bg"))
        video_row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(video_row, text="생성 개수", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.video_variant_combo = self._choice_menu(video_row, self.video_variant_var, ("x1", "x2", "x3", "x4"))
        self.video_variant_combo.pack(side="left", padx=(12, 12))
        tk.Label(video_row, text="비디오 화질", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.video_quality_combo = self._choice_menu(video_row, self.video_quality_var, ("720P", "1080P", "4K"))
        self.video_quality_combo.pack(side="left", padx=(12, 0))

        mode_row = tk.Frame(parent, bg=self._bg("settings_bg"))
        mode_row.pack(fill="x", padx=4, pady=(0, 6))
        for text, value in (("전체", "all"), ("연속", "range"), ("개별", "manual")):
            tk.Radiobutton(mode_row, text=text, value=value, variable=self.number_mode_var, command=self.on_number_mode_changed, bg=self._bg("settings_bg"), fg="#FFFFFF", selectcolor=self._bg("chip_bg"), activebackground=self._bg("settings_bg"), activeforeground="#FFFFFF", font=("Malgun Gothic", 10)).pack(side="left", padx=(0, 18))

        range_row = tk.Frame(parent, bg=self._bg("settings_bg"))
        range_row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(range_row, text="연속 범위", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.start_entry = tk.Entry(range_row, textvariable=self.start_number_var, width=8, font=("Consolas", 12))
        self.start_entry.pack(side="left", padx=(12, 6))
        self._bind_entry_autosave(self.start_entry, "번호 범위 변경")
        tk.Label(range_row, text="~", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.end_entry = tk.Entry(range_row, textvariable=self.end_number_var, width=8, font=("Consolas", 12))
        self.end_entry.pack(side="left", padx=(6, 0))
        self._bind_entry_autosave(self.end_entry, "번호 범위 변경")

        manual_row = tk.Frame(parent, bg=self._bg("settings_bg"))
        manual_row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(manual_row, text="개별 번호", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(anchor="w")
        self.manual_entry = tk.Entry(manual_row, textvariable=self.manual_numbers_var, font=("Consolas", 12))
        self.manual_entry.pack(fill="x", pady=(6, 0))
        self._bind_entry_autosave(self.manual_entry, "개별 번호 변경")

        speed_row = tk.Frame(parent, bg=self._bg("settings_bg"))
        speed_row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(speed_row, text="타이핑 속도", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(anchor="w")
        self.speed_scale = tk.Scale(
            speed_row,
            from_=0.5,
            to=2.0,
            resolution=0.1,
            orient="horizontal",
            variable=self.typing_speed_var,
            bg=self._bg("settings_bg"),
            fg="#FFFFFF",
            troughcolor=self._bg("chip_bg"),
            highlightthickness=0,
            command=lambda _v: self.auto_save("타이핑 속도 변경"),
        )
        self.speed_scale.pack(fill="x")

        humanize_row = tk.Frame(parent, bg=self._bg("settings_bg"))
        humanize_row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(humanize_row, text="인간처럼 입력", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        tk.Label(humanize_row, text="항상 ON", bg=self._bg("chip_bg"), fg=self._bg("status_fg"), font=("Malgun Gothic", 10, "bold"), padx=10, pady=3).pack(side="left", padx=(10, 0))

        wait_row_1 = tk.Frame(parent, bg=self._bg("settings_bg"))
        wait_row_1.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(wait_row_1, text="생성 후 다운로드 대기(초)", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.generate_wait_entry = tk.Entry(wait_row_1, textvariable=self.generate_wait_var, width=8, font=("Consolas", 11))
        self.generate_wait_entry.pack(side="left", padx=(12, 0))
        self._bind_entry_autosave(self.generate_wait_entry, "생성 대기시간 변경")

        wait_row_2 = tk.Frame(parent, bg=self._bg("settings_bg"))
        wait_row_2.pack(fill="x", padx=4, pady=(0, 4))
        tk.Label(wait_row_2, text="다운로드 후 다음 작업 대기(초)", bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(side="left")
        self.next_prompt_wait_entry = tk.Entry(wait_row_2, textvariable=self.next_wait_var, width=8, font=("Consolas", 11))
        self.next_prompt_wait_entry.pack(side="left", padx=(12, 0))
        self._bind_entry_autosave(self.next_prompt_wait_entry, "다음 작업 대기시간 변경")

    def _choice_menu(self, parent, variable: tk.StringVar, values):
        menu = tk.OptionMenu(parent, variable, *values)
        menu.configure(font=("Malgun Gothic", 10), bg="#F6E3CA", width=7, highlightthickness=0)
        variable.trace_add("write", lambda *_: self.auto_save("선택값 변경"))
        return menu

    def _labeled_combo(self, parent: tk.Frame, label: str, variable: tk.StringVar, callback) -> None:
        row = tk.Frame(parent, bg=self._bg("settings_bg"))
        row.pack(fill="x", padx=4, pady=(0, 6))
        tk.Label(row, text=label, bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(anchor="w")
        combo = tk.OptionMenu(row, variable, "")
        combo.configure(font=("Malgun Gothic", 10), bg="#F6E3CA", width=38, highlightthickness=0)
        combo.pack(fill="x", pady=(6, 0))
        variable.trace_add("write", lambda *_: callback())
        if label == "프롬프트 파일":
            self.prompt_menu = combo
        else:
            self.project_menu = combo

    def _path_row(self, parent: tk.Frame, label: str, variable: tk.StringVar, command) -> None:
        row = tk.Frame(parent, bg=self._bg("settings_bg"))
        row.pack(fill="x", padx=4, pady=(0, 8))
        tk.Label(row, text=label, bg=self._bg("settings_bg"), fg="#D8E4FF", font=("Malgun Gothic", 10)).pack(anchor="w")
        input_row = tk.Frame(row, bg=self._bg("settings_bg"))
        input_row.pack(fill="x", pady=(6, 0))
        entry = tk.Entry(input_row, textvariable=variable, font=("Consolas", 10))
        entry.pack(side="left", fill="x", expand=True)
        self._bind_entry_autosave(entry, f"{label} 변경")
        self._action_button(input_row, "선택", command, self._bg("open_btn_bg"), small=True).pack(side="left", padx=(8, 0))

    def _bind_entry_autosave(self, widget, reason: str) -> None:
        widget.bind("<FocusOut>", lambda _e, r=reason: self.auto_save(r))
        widget.bind("<Return>", lambda _e, r=reason: self.auto_save(r))

    def _action_button(self, parent, text, command, bg, small=False, width=None):
        return tk.Button(parent, text=text, command=command, bg=bg, fg="#FFFFFF", activebackground=bg, activeforeground="#FFFFFF", relief="flat", padx=14 if not small else 10, pady=8 if not small else 5, font=("Malgun Gothic", 10, "bold" if not small else "normal"), width=width, cursor="hand2")

    def _load_vars_from_config(self) -> None:
        self.worker_name_var.set(str(self.cfg.get("worker_name") or "Flow Worker1"))
        slots = self.cfg.get("prompt_slots") or []
        profiles = self.cfg.get("project_profiles") or []
        slot_index = max(0, min(int(self.cfg.get("prompt_slot_index", 0) or 0), len(slots) - 1)) if slots else 0
        project_index = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1)) if profiles else 0
        self.prompt_slot_var.set(str((slots[slot_index] or {}).get("name") or ""))
        self.project_var.set(str((profiles[project_index] or {}).get("name") or ""))
        self.download_dir_var.set(str(self.cfg.get("download_output_dir") or ""))
        self.media_mode_var.set(str(self.cfg.get("media_mode") or "image"))
        self.number_mode_var.set(str(self.cfg.get("number_mode") or "all"))
        self.start_number_var.set(str(self.cfg.get("start_number", 1) or 1))
        self.end_number_var.set(str(self.cfg.get("end_number", 10) or 10))
        self.manual_numbers_var.set(str(self.cfg.get("manual_numbers") or ""))
        self.image_variant_var.set(str(self.cfg.get("image_variant_count") or "x1"))
        self.video_variant_var.set(str(self.cfg.get("video_variant_count") or "x1"))
        self.image_quality_var.set(str(self.cfg.get("image_quality") or "1K"))
        self.video_quality_var.set(str(self.cfg.get("video_quality") or "1080P"))
        self.typing_speed_var.set(float(self.cfg.get("typing_speed", 1.0) or 1.0))
        self.humanize_typing_var.set(True)
        self.generate_wait_var.set(str(self.cfg.get("generate_wait_seconds", 10.0) or 10.0))
        self.next_wait_var.set(str(self.cfg.get("next_prompt_wait_seconds", 7.0) or 7.0))
        self.attach_url_var.set(f"FlowWorker 전용 Edge | {self.cfg.get('browser_attach_url', 'http://127.0.0.1:9333')}")

    def _write_vars_to_config(self) -> None:
        self.cfg["worker_name"] = self.worker_name_var.get().strip() or "Flow Worker1"
        self.cfg["download_output_dir"] = self.download_dir_var.get().strip()
        self.cfg["media_mode"] = self.media_mode_var.get().strip() or "image"
        self.cfg["number_mode"] = self.number_mode_var.get().strip() or "all"
        self.cfg["start_number"] = self._int_or_default(self.start_number_var.get(), 1)
        self.cfg["end_number"] = self._int_or_default(self.end_number_var.get(), self.cfg["start_number"])
        self.cfg["manual_numbers"] = self.manual_numbers_var.get().strip()
        self.cfg["image_variant_count"] = self.image_variant_var.get().strip() or "x1"
        self.cfg["video_variant_count"] = self.video_variant_var.get().strip() or "x1"
        self.cfg["image_quality"] = self.image_quality_var.get().strip().upper() or "1K"
        self.cfg["video_quality"] = self.video_quality_var.get().strip().upper() or "1080P"
        self.cfg["typing_speed"] = round(float(self.typing_speed_var.get() or 1.0), 1)
        self.cfg["humanize_typing"] = True
        self.cfg["generate_wait_seconds"] = self._float_or_default(self.generate_wait_var.get(), 10.0)
        self.cfg["next_prompt_wait_seconds"] = self._float_or_default(self.next_wait_var.get(), 7.0)
        self.cfg["window_geometry"] = self.root.geometry()
        self.cfg["settings_collapsed"] = bool(self.settings_collapsed)
        self.cfg["log_panel_visible"] = bool(self.log_panel_visible)

        slot_names = [str((slot or {}).get("name") or "") for slot in self.cfg.get("prompt_slots") or []]
        project_names = [str((item or {}).get("name") or "") for item in self.cfg.get("project_profiles") or []]
        if self.prompt_slot_var.get().strip() in slot_names:
            self.cfg["prompt_slot_index"] = slot_names.index(self.prompt_slot_var.get().strip())
        if self.project_var.get().strip() in project_names:
            self.cfg["project_index"] = project_names.index(self.project_var.get().strip())

    def auto_save(self, reason: str = "") -> None:
        if self._suspend_auto_save:
            return
        self._write_vars_to_config()
        save_config(self.base_dir, self.cfg, CONFIG_FILE)
        if reason:
            self.log(f"자동 저장: {reason}")
        self.refresh_summary_only()

    def manual_save(self) -> None:
        self._write_vars_to_config()
        save_config(self.base_dir, self.cfg, CONFIG_FILE)
        self.log("설정 저장")
        self.refresh_summary_only()

    def refresh_all(self) -> None:
        self._refresh_project_menu()
        self._refresh_prompt_menu()
        self.on_number_mode_changed()
        self._apply_settings_visibility()
        self._apply_media_visibility()
        self._apply_log_panel_visibility()
        self.refresh_summary_only()
        self._render_queue()

    def toggle_settings_panel(self) -> None:
        self.settings_collapsed = not self.settings_collapsed
        self._apply_settings_visibility()
        self.auto_save("설정 접기/펼치기 변경")

    def toggle_log_panel(self) -> None:
        self.log_panel_visible = not self.log_panel_visible
        self._apply_log_panel_visibility()
        self.auto_save("로그 패널 표시 변경")

    def on_media_mode_changed(self) -> None:
        self._apply_media_visibility()
        self.start_btn.config(text="▶ 비디오 시작" if self.media_mode_var.get().strip() == "video" else "▶ 이미지 시작")
        if not self._suspend_auto_save:
            self.auto_save("작업 모드 변경")

    def on_number_mode_changed(self) -> None:
        self.refresh_summary_only()
        if not self._suspend_auto_save:
            self.auto_save("번호 모드 변경")

    def _apply_settings_visibility(self) -> None:
        if self.settings_collapsed:
            try:
                self.settings_frame.pack_forget()
            except Exception:
                pass
            self.settings_toggle_btn.configure(text="⚙ 설정 펼치기")
        else:
            if not self.settings_frame.winfo_manager():
                self.settings_frame.pack(fill="x", before=self.lower_frame)
            self.settings_toggle_btn.configure(text="⚙ 설정 접기")

    def _apply_log_panel_visibility(self) -> None:
        if self.log_panel_visible:
            if not self.log_frame.winfo_manager():
                self.log_frame.pack(side="right", fill="both", padx=(8, 0))
            self.log_toggle_btn.configure(text="로그 숨기기")
        else:
            if self.log_frame.winfo_manager():
                self.log_frame.pack_forget()
            self.log_toggle_btn.configure(text="로그 보기")
            self.root.after(50, self._render_queue)

    def _apply_media_visibility(self) -> None:
        if self.media_mode_var.get().strip() == "video":
            if not self.video_settings_frame.winfo_manager():
                self.video_settings_frame.pack(fill="x", padx=0, pady=(0, 2))
            try:
                self.image_settings_frame.pack_forget()
            except Exception:
                pass
        else:
            if not self.image_settings_frame.winfo_manager():
                self.image_settings_frame.pack(fill="x", padx=0, pady=(0, 2))
            try:
                self.video_settings_frame.pack_forget()
            except Exception:
                pass

    def _refresh_project_menu(self) -> None:
        menu = self.project_menu["menu"]
        menu.delete(0, "end")
        values = [str((item or {}).get("name") or f"프로젝트 {idx+1}") for idx, item in enumerate(self.cfg.get("project_profiles") or [])]
        for value in values:
            menu.add_command(label=value, command=lambda v=value: self.project_var.set(v))
        if values and self.project_var.get().strip() not in values:
            self.project_var.set(values[0])

    def _refresh_prompt_menu(self) -> None:
        menu = self.prompt_menu["menu"]
        menu.delete(0, "end")
        values = [str((slot or {}).get("name") or f"프롬프트 {idx+1}") for idx, slot in enumerate(self.cfg.get("prompt_slots") or [])]
        for value in values:
            menu.add_command(label=value, command=lambda v=value: self.prompt_slot_var.set(v))
        if values and self.prompt_slot_var.get().strip() not in values:
            self.prompt_slot_var.set(values[0])

    def refresh_summary_only(self) -> None:
        profiles = list(self.cfg.get("project_profiles") or [])
        slots = list(self.cfg.get("prompt_slots") or [])
        project_index = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1)) if profiles else 0
        prompt_index = max(0, min(int(self.cfg.get("prompt_slot_index", 0) or 0), len(slots) - 1)) if slots else 0
        project = profiles[project_index] if profiles else {"name": "기본 프로젝트", "url": self.cfg.get("flow_site_url", "")}
        media_label = "비디오" if str(self.cfg.get("media_mode") or "image") == "video" else "이미지"
        self.project_summary_var.set(f"사이트: flow | {media_label} | {project.get('name', '기본 프로젝트')}")
        self.attach_url_var.set(f"FlowWorker 전용 Edge | {self.cfg.get('browser_attach_url', 'http://127.0.0.1:9333')}")
        if slots:
            slot_file = str((slots[prompt_index] or {}).get("file") or "")
            slot_path = self.base_dir / slot_file
            summary = summarize_prompt_file(
                slot_path,
                prefix=str(self.cfg.get("prompt_prefix") or "S"),
                pad_width=int(self.cfg.get("prompt_pad_width", 3) or 3),
                separator=str(self.cfg.get("prompt_separator") or "|||"),
                extra_prefixes=("V",) if str(self.cfg.get("media_mode") or "image") == "video" else (),
            )
            self.prompt_file_summary_var.set(self._format_prompt_summary_for_ui(summary))
        else:
            self.prompt_file_summary_var.set("프롬프트 파일 없음")
        self.root.title(f"Flow Worker - {self.cfg.get('worker_name', 'Flow Worker1')}")

    def _format_prompt_summary_for_ui(self, summary: str, *, max_lines: int = 3, max_chars: int = 92) -> str:
        raw = str(summary or "").strip()
        if len(raw) <= max_chars * max_lines:
            return raw
        return raw[: max_chars * max_lines - 3].rstrip(", ") + "..."

    def project_changed(self) -> None:
        if not self._suspend_auto_save:
            self.auto_save("프로젝트 변경")

    def prompt_slot_changed(self) -> None:
        if not self._suspend_auto_save:
            self.auto_save("프롬프트 파일 변경")

    def add_project(self) -> None:
        name = simpledialog.askstring("프로젝트 추가", "프로젝트 이름을 적어주세요:", parent=self.root)
        if not name:
            return
        url = simpledialog.askstring("프로젝트 URL", "Flow 프로젝트 URL을 적어주세요:", parent=self.root)
        if not url:
            return
        profiles = list(self.cfg.get("project_profiles") or [])
        profiles.append({"name": name.strip(), "url": url.strip()})
        self.cfg["project_profiles"] = profiles
        self.cfg["project_index"] = len(profiles) - 1
        self.project_var.set(name.strip())
        self.manual_save()
        self.refresh_all()

    def rename_project(self) -> None:
        profiles = list(self.cfg.get("project_profiles") or [])
        if not profiles:
            return
        idx = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1))
        current = str((profiles[idx] or {}).get("name") or "")
        new_name = simpledialog.askstring("프로젝트 이름수정", "새 프로젝트 이름을 적어주세요:", initialvalue=current, parent=self.root)
        if not new_name:
            return
        profiles[idx]["name"] = new_name.strip()
        self.cfg["project_profiles"] = profiles
        self.project_var.set(new_name.strip())
        self.manual_save()
        self.refresh_all()

    def edit_project_url(self) -> None:
        profiles = list(self.cfg.get("project_profiles") or [])
        if not profiles:
            return
        idx = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1))
        current = str((profiles[idx] or {}).get("url") or "")
        new_url = simpledialog.askstring("프로젝트 URL 편집", "Flow 프로젝트 URL을 적어주세요:", initialvalue=current, parent=self.root)
        if not new_url:
            return
        profiles[idx]["url"] = new_url.strip()
        self.cfg["project_profiles"] = profiles
        self.manual_save()
        self.refresh_all()

    def delete_project(self) -> None:
        profiles = list(self.cfg.get("project_profiles") or [])
        if len(profiles) <= 1:
            messagebox.showwarning("삭제 불가", "프로젝트는 최소 1개는 남아 있어야 합니다.", parent=self.root)
            return
        idx = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1))
        name = str((profiles[idx] or {}).get("name") or "")
        if not messagebox.askyesno("프로젝트 삭제", f"'{name}' 프로젝트를 삭제할까요?", parent=self.root):
            return
        profiles.pop(idx)
        self.cfg["project_profiles"] = profiles
        self.cfg["project_index"] = max(0, min(idx, len(profiles) - 1))
        self.manual_save()
        self.refresh_all()

    def open_prompt_file(self) -> None:
        slot_path = self._current_prompt_path()
        if not slot_path:
            return
        _open_path(slot_path)

    def rename_prompt_file(self) -> None:
        slots = list(self.cfg.get("prompt_slots") or [])
        if not slots:
            return
        idx = max(0, min(int(self.cfg.get("prompt_slot_index", 0) or 0), len(slots) - 1))
        current = str((slots[idx] or {}).get("name") or "")
        new_name = simpledialog.askstring("프롬프트 파일 이름수정", "새 이름을 적어주세요:", initialvalue=current, parent=self.root)
        if not new_name:
            return
        slots[idx]["name"] = new_name.strip()
        self.cfg["prompt_slots"] = slots
        self.prompt_slot_var.set(new_name.strip())
        self.manual_save()
        self.refresh_all()

    def add_prompt_file(self) -> None:
        new_name = simpledialog.askstring("프롬프트 파일 추가", "새 파일 이름을 적어주세요:", parent=self.root)
        if not new_name:
            return
        new_file = next_prompt_slot_file(self.base_dir, list(self.cfg.get("prompt_slots") or []))
        target = self.base_dir / new_file
        target.write_text("", encoding="utf-8")
        slots = list(self.cfg.get("prompt_slots") or [])
        slots.append({"name": new_name.strip(), "file": new_file})
        self.cfg["prompt_slots"] = slots
        self.cfg["prompt_slot_index"] = len(slots) - 1
        self.prompt_slot_var.set(new_name.strip())
        self.manual_save()
        self.refresh_all()
        _open_path(target)

    def delete_prompt_file(self) -> None:
        slots = list(self.cfg.get("prompt_slots") or [])
        if len(slots) <= 1:
            messagebox.showwarning("삭제 불가", "프롬프트 파일은 최소 1개는 남아 있어야 합니다.", parent=self.root)
            return
        idx = max(0, min(int(self.cfg.get("prompt_slot_index", 0) or 0), len(slots) - 1))
        name = str((slots[idx] or {}).get("name") or "")
        if not messagebox.askyesno("프롬프트 파일 삭제", f"'{name}' 파일을 목록에서 지울까요?\n실제 txt 파일은 남겨둡니다.", parent=self.root):
            return
        slots.pop(idx)
        self.cfg["prompt_slots"] = slots
        self.cfg["prompt_slot_index"] = max(0, min(idx, len(slots) - 1))
        self.manual_save()
        self.refresh_all()

    def choose_download_dir(self) -> None:
        current = self.download_dir_var.get().strip() or str((self.base_dir / "downloads").resolve())
        chosen = filedialog.askdirectory(initialdir=current, title="저장 폴더 선택")
        if not chosen:
            return
        self.download_dir_var.set(chosen)
        self.auto_save("저장 폴더 변경")

    def copy_prompt_numbers(self) -> None:
        plan = FlowAutomationEngine(self.base_dir, self.cfg).build_plan()
        prefix = "V" if str(self.cfg.get("media_mode") or "image") == "video" else "S"
        text = compress_numbers([item.number for item in plan.items], prefix=prefix)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"번호 복사: {text or '(비어 있음)'}")

    def copy_failed_numbers(self) -> None:
        failed = [item.tag for item in self.queue_items if item.status == "failed"]
        text = ",".join(failed)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"실패 번호 복붙: {text or '(비어 있음)'}")

    def open_browser_window(self) -> None:
        self.auto_save("브라우저 열기 전 저장")
        project = self._current_project()
        if not project.get("url"):
            messagebox.showwarning("안내", "프로젝트 URL을 먼저 입력해주세요.", parent=self.root)
            return
        try:
            self.log(
                f"브라우저 열기 요청 | 프로필={self.cfg.get('browser_profile_dir')} | "
                f"포트={self.cfg.get('browser_attach_url')}"
            )
            self.browser.open_project(
                url=str(project.get("url") or ""),
                profile_dir=str(self.base_dir / str(self.cfg.get("browser_profile_dir") or "runtime/flow_worker_edge_profile")),
                attach_url=str(self.cfg.get("browser_attach_url") or "http://127.0.0.1:9333"),
                window_cfg=self.cfg,
            )
            self.status_var.set("브라우저 준비 완료")
            self.log("브라우저 작업봇 창 열기 완료")
        except Exception as exc:
            self.status_var.set("브라우저 열기 실패")
            self.log(f"브라우저 열기 실패: {exc}")
            messagebox.showerror("브라우저 열기 실패", str(exc), parent=self.root)

    def start_run(self) -> None:
        if self.run_thread and self.run_thread.is_alive():
            self.log("이미 작업 스레드가 실행 중입니다.")
            return
        self.stop_requested = False
        self.paused = False
        self.auto_save("시작 전 저장")
        plan = FlowAutomationEngine(self.base_dir, self.cfg).build_plan()
        self.queue_items = [QueueItem(number=item.number, tag=item.tag, prompt=item.rendered_prompt) for item in plan.items]
        self._render_queue()
        self._refresh_progress_from_plan(plan.items)
        self.status_var.set("실행 준비 중")
        self.log(f"실행 시작: {plan.selection_summary}")

        def _run() -> None:
            try:
                engine = FlowAutomationEngine(self.base_dir, self.cfg)
                engine.run(
                    plan=plan,
                    log=self.log,
                    set_status=self._threadsafe_status,
                    update_queue=self._threadsafe_queue_update,
                    should_stop=lambda: self.stop_requested,
                    is_paused=lambda: self.paused,
                    browser=self.browser,
                )
            except Exception as exc:
                self._threadsafe_status("실행 실패")
                self.log(f"실행 실패: {exc}")

        self.run_thread = threading.Thread(target=_run, daemon=True)
        self.run_thread.start()

    def stop_all(self) -> None:
        self.stop_requested = True
        self.status_var.set("중지됨")
        self.log("완전정지")
        self.browser.stop(close_window=False)

    def pause_run(self) -> None:
        self.paused = True
        self.status_var.set("일시정지")
        self.log("일시정지")

    def resume_run(self) -> None:
        self.paused = False
        self.status_var.set("재개됨")
        self.log("재개")

    def _threadsafe_status(self, text: str) -> None:
        self.root.after(0, lambda value=text: self.status_var.set(value))

    def _threadsafe_queue_update(self, number: int, status: str, message: str, file_name: str) -> None:
        def _apply() -> None:
            for item in self.queue_items:
                if item.number == number:
                    item.status = status
                    item.message = message
                    item.file_name = file_name
                    break
            self._render_queue()

        self.root.after(0, _apply)

    def _current_project(self) -> dict:
        profiles = list(self.cfg.get("project_profiles") or [])
        idx = max(0, min(int(self.cfg.get("project_index", 0) or 0), len(profiles) - 1)) if profiles else 0
        return profiles[idx] if profiles else {"name": "기본 프로젝트", "url": self.cfg.get("flow_site_url", "")}

    def _current_prompt_path(self) -> Path | None:
        slots = list(self.cfg.get("prompt_slots") or [])
        if not slots:
            return None
        idx = max(0, min(int(self.cfg.get("prompt_slot_index", 0) or 0), len(slots) - 1))
        return self.base_dir / str((slots[idx] or {}).get("file") or "")

    def _refresh_progress_from_plan(self, items) -> None:
        total = len(list(items or []))
        self.progress_var.set(f"0 / {total} (0.0%)" if total > 0 else "0 / 0 (0.0%)")
        self.progress_canvas.coords(self.progress_fill, 0, 0, 0, 18)

    def _render_queue(self) -> None:
        for child in self.queue_inner.winfo_children():
            child.destroy()
        active = 0
        success = 0
        failed = 0
        pending = 0
        if not self.queue_items:
            tk.Label(self.queue_inner, text="아직 대기열이 없습니다.\n작업봇 창 열기나 시작을 누르면 여기에 상태가 쌓입니다.", bg=self._bg("queue_panel_bg"), fg=self._bg("sub_fg"), justify="left").pack(anchor="w", padx=10, pady=10)
        else:
            for item in self.queue_items:
                status = str(item.status or "pending").strip().lower()
                if status in ("running", "waiting", "downloading"):
                    active += 1
                elif status == "success":
                    success += 1
                elif status == "failed":
                    failed += 1
                else:
                    pending += 1
                color = {
                    "pending": "#433124",
                    "running": "#7D522E",
                    "waiting": "#6E5B45",
                    "downloading": "#2F6B53",
                    "success": "#2A6F4C",
                    "failed": "#7E3842",
                }.get(status, "#433124")
                card = tk.Frame(self.queue_inner, bg=color, highlightbackground=self._bg("queue_panel_border"), highlightthickness=1)
                card.pack(fill="x", pady=(0, 8))
                tk.Label(card, text=item.tag, bg=color, fg="#FFFFFF", font=("Malgun Gothic", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
                detail = item.message or item.prompt
                tk.Label(card, text=detail[:160], bg=color, fg="#E8F1FF", justify="left", wraplength=620).pack(anchor="w", padx=10, pady=(0, 8))
        self.queue_summary_var.set(f"활성 {active}개 | 완료 {success} | 실패 {failed} | 대기 {pending}")
        self._refresh_progress_from_queue()
        self._update_queue_scroll()

    def _render_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", "\n".join(self.log_lines[-120:]))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _update_queue_scroll(self) -> None:
        self.queue_canvas.configure(scrollregion=self.queue_canvas.bbox("all"))

    def _on_queue_canvas_resize(self, event) -> None:
        self.queue_canvas.itemconfigure(self.queue_window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        try:
            self.queue_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass

    def log(self, message: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, lambda msg=message: self.log(msg))
            return
        self.log_lines.append(message)
        self.log_lines = self.log_lines[-200:]
        self._render_log()

    def _refresh_progress_from_queue(self) -> None:
        total = len(self.queue_items)
        if total <= 0:
            self.progress_var.set("0 / 0 (0.0%)")
            self.progress_canvas.coords(self.progress_fill, 0, 0, 0, 18)
            return
        done = 0
        for item in self.queue_items:
            if str(item.status or "").strip().lower() in {"success", "failed"}:
                done += 1
        ratio = max(0.0, min(1.0, done / total))
        self.progress_var.set(f"{done} / {total} ({ratio * 100:.1f}%)")
        width = int(round(230 * ratio))
        self.progress_canvas.coords(self.progress_fill, 0, 0, width, 18)

    def _start_resize_drag(self, event) -> None:
        self._resize_drag_origin = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _on_resize_drag(self, event) -> None:
        if not self._resize_drag_origin:
            return
        start_x, start_y, start_w, start_h = self._resize_drag_origin
        new_w = max(900, start_w + (event.x_root - start_x))
        new_h = max(560, start_h + (event.y_root - start_y))
        self.root.geometry(f"{new_w}x{new_h}")

    def _end_resize_drag(self, _event=None) -> None:
        self._resize_drag_origin = None

    def on_close(self) -> None:
        self.manual_save()
        self.browser.stop(close_window=False)
        self.root.destroy()

    @staticmethod
    def _int_or_default(value: str, default: int) -> int:
        try:
            return int(str(value).strip())
        except Exception:
            return int(default)

    @staticmethod
    def _float_or_default(value: str, default: float) -> float:
        try:
            return float(str(value).strip())
        except Exception:
            return float(default)
