from __future__ import annotations

import math
import random
import time
from datetime import datetime
from typing import Callable


QWERTY_NEIGHBORS = {
    "1": "2q", "2": "13qw", "3": "24we", "4": "35er", "5": "46rt", "6": "57ty", "7": "68yu", "8": "79ui", "9": "80io", "0": "9-op",
    "q": "12wa", "w": "qeas23", "e": "wrsd34", "r": "etdf45", "t": "ryfg56", "y": "tugh67", "u": "yihj78", "i": "uojk89", "o": "ipkl90", "p": "ol0-",
    "a": "qwsz", "s": "qweadz", "d": "wersfc", "f": "ertdgv", "g": "rtyfhb", "h": "tyugjn", "j": "yuihkm", "k": "uiojlm", "l": "opk",
    "z": "asx", "x": "zsdc", "c": "xdfv", "v": "cfgb", "b": "vghn", "n": "bhjm", "m": "njk",
}

KOREAN_TYPO_POOL = "가나다라마바사아자차카타파하은는이가을를에의고다요"


class HumanActor:
    def __init__(self, action_logger: Callable[[str], None] | None = None, status_callback: Callable[[str], None] | None = None) -> None:
        self.page = None
        self.action_logger = action_logger
        self.status_callback = status_callback
        self.session_start_time = time.time()
        self.mouse_x = 960.0
        self.mouse_y = 540.0
        self._viewport_cache = (1920, 1080)
        self.typing_speed_profile = "x5"
        self.typing_speed_level = 5
        self.typing_speed_factor = 1.0
        self.action_delay_factor = 1.0
        self.thinking_delay_factor = 1.0
        self.randomize_persona()
        self.set_typing_speed_profile("x5")

    def set_page(self, page) -> None:
        self.page = page
        width, height = self._viewport()
        self.mouse_x = width / 2
        self.mouse_y = height / 2
        self._log_action(f"브라우저 페이지 연결됨 (viewport={width}x{height})")

    def set_typing_speed_profile(self, profile: str) -> None:
        raw = str(profile or "x5").strip().lower()
        legacy = {"slow": "x2", "normal": "x5", "fast": "x10", "turbo": "x16"}
        raw = legacy.get(raw, raw)
        level = 5
        if raw.startswith("x") and raw[1:].isdigit():
            level = max(1, min(20, int(raw[1:])))
        self.typing_speed_level = level
        self.typing_speed_profile = f"x{level}"
        self.typing_speed_factor = 0.65 + ((level - 1) / 19.0) * 5.35
        self.action_delay_factor = max(0.12, 1.45 - ((level - 1) / 19.0) * 1.33)
        self.thinking_delay_factor = max(0.10, 1.60 - ((level - 1) / 19.0) * 1.50)

    def randomize_persona(self) -> None:
        mood = random.choice(["energetic", "calm", "tired", "meticulous"])
        base_speed = random.uniform(0.8, 1.2)
        if mood == "energetic":
            base_speed *= 1.2
        elif mood == "tired":
            base_speed *= 0.8
        self.cfg = {
            "speed_multiplier": base_speed,
            "overshoot_rate": random.uniform(0.08, 0.24),
            "hesitation_before_click": random.uniform(0.28, 0.62),
            "typo_rate": random.uniform(0.012, 0.032),
            "breathing_pause_rate": random.uniform(0.18, 0.42),
            "sentence_pause_rate": random.uniform(0.55, 0.86),
            "burst_pause_rate": random.uniform(0.12, 0.28),
            "mouse_wiggle_rate": random.uniform(0.08, 0.22),
        }
        self.current_mood = mood
        self.current_persona_name = f"Flow human {random.randint(1000, 9999)}"

    def get_fatigue_factor(self) -> float:
        elapsed_min = (time.time() - self.session_start_time) / 60.0
        if elapsed_min <= 30:
            return 1.0
        return 1.0 - min(0.2, (elapsed_min - 30) * 0.005)

    def random_action_delay(self, reason: str = "행동 딜레이", min_sec: float = 0.3, max_sec: float = 2.0) -> float:
        delay = random.uniform(min_sec, max_sec) * self.action_delay_factor * random.uniform(0.7, 1.3)
        delay = max(0.03, delay)
        self._log_action(f"{reason}: {delay:.2f}초 대기")
        time.sleep(delay)
        return delay

    def move_to_locator(self, locator, label: str = "대상") -> None:
        if self.page is None:
            raise RuntimeError("Playwright page가 연결되지 않았습니다.")
        box = locator.bounding_box()
        if not box:
            raise RuntimeError(f"{label} 요소 위치를 찾지 못했습니다.")
        tx = float(box["x"]) + float(box["width"]) / 2.0 + random.uniform(-3.0, 3.0)
        ty = float(box["y"]) + float(box["height"]) / 2.0 + random.uniform(-2.0, 2.0)
        self._log_action(f"마우스 이동 -> {label} ({tx:.1f}, {ty:.1f})")
        self.move_to(tx, ty)

    def smart_click(self, label: str = "클릭", button: str = "left") -> None:
        if self.page is None:
            return
        self.random_action_delay(f"{label} 전 딜레이", 0.04, 0.18)
        hold = random.uniform(0.035, 0.16)
        self.page.mouse.down(button=button)
        time.sleep(hold)
        self.page.mouse.up(button=button)
        self._log_action(f"{label} 실행 (hold={hold:.2f}s)")
        self.random_action_delay(f"{label} 후 딜레이", 0.08, 0.32)

    def clear_input_field(self, locator, label: str = "입력창") -> None:
        self.move_to_locator(locator, label=label)
        self.smart_click(label=f"{label} 포커스")
        self.page.keyboard.press("Control+A")
        self.random_action_delay("전체선택 후 딜레이", 0.05, 0.18)
        self.page.keyboard.press("Backspace")
        self._log_action(f"{label} 내용 초기화")

    def type_text(self, text: str, input_locator=None, mode: str = "typing") -> None:
        if self.page is None:
            raise RuntimeError("Playwright page가 연결되지 않았습니다.")
        if input_locator is not None:
            self.move_to_locator(input_locator, "입력창")
            self.smart_click("입력창 클릭")
        chosen_mode = random.choice(["typing", "paste"]) if mode == "mixed" else mode
        self._log_action(f"텍스트 입력 시작 (mode={chosen_mode}, len={len(text)})")
        if chosen_mode == "paste":
            self.random_action_delay("붙여넣기 전 딜레이", 0.06, 0.18)
            self.page.keyboard.insert_text(text)
            self.random_action_delay("붙여넣기 후 딜레이", 0.06, 0.18)
            self._log_action("붙여넣기 완료")
            return
        fatigue = self.get_fatigue_factor()
        typo_rate = float(self.cfg.get("typo_rate", 0.02))
        next_burst_at = random.randint(9, 22)
        recent_word_chars = 0
        self.think_pause("입력 전 짧은 생각", 0.12, 0.55)
        for idx, ch in enumerate(str(text or "")):
            if idx > 0 and idx >= next_burst_at and random.random() < float(self.cfg.get("burst_pause_rate", 0.18)):
                self.think_pause("입력 중 멈칫", 0.10, 0.42)
                next_burst_at = idx + random.randint(10, 28)
            if ch not in (" ", "\n") and random.random() < typo_rate:
                self._handle_typo(ch)
            if ch == "\n":
                self.page.keyboard.press("Shift+Enter")
            else:
                self.page.keyboard.type(ch)
            speed = max(0.45, min(float(self.cfg.get("speed_multiplier", 1.0)) * self.typing_speed_factor * random.uniform(0.7, 1.3), 8.0))
            fatigue_slow = 1.0 + max(0.0, 1.0 - fatigue) * 0.45
            if ch == "\n":
                delay = random.uniform(0.20, 0.70)
            elif ch in (" ", "\t"):
                delay = random.uniform(0.035, 0.14)
                recent_word_chars = 0
            elif ch in ".,!?:;)(" or ch in "。！？、，；：":
                delay = random.uniform(0.11, 0.38)
                if random.random() < float(self.cfg.get("sentence_pause_rate", 0.7)):
                    delay += random.uniform(0.15, 0.55)
                recent_word_chars = 0
            else:
                recent_word_chars += 1
                if recent_word_chars <= 2:
                    delay = random.uniform(0.045, 0.16)
                else:
                    delay = random.uniform(0.028, 0.12)
                if ch in "은는이가을를에의고다요":
                    delay += random.uniform(0.015, 0.09)
            self._jitter_mouse_during_typing()
            time.sleep(max(0.012, min(delay * (1.0 / speed) * fatigue_slow, 0.72)))
        self._log_action("타이핑 완료")

    def read_prompt_pause(self, text: str) -> None:
        units = max(1, min(8, len(str(text or "")) // 180))
        self.think_pause("프롬프트 검토", 0.4, 1.2 + units * 0.25)

    def hesitate_on_submit(self) -> None:
        self.think_pause("제출 전 망설임", 0.4, 1.6)

    def think_pause(self, reason: str, min_sec: float, max_sec: float) -> float:
        delay = random.uniform(min_sec, max_sec) * self.thinking_delay_factor * random.uniform(0.7, 1.3)
        delay = max(0.08, delay)
        self._log_action(f"{reason}: {delay:.2f}초")
        if self.status_callback:
            self.status_callback(f"{reason} ({delay:.1f}초)")
        time.sleep(delay)
        return delay

    def move_to(self, tx: float, ty: float) -> None:
        if self.page is None:
            return
        tx, ty = self._clamp(tx, ty)
        sx, sy = self.mouse_x, self.mouse_y
        dist = math.hypot(tx - sx, ty - sy)
        duration = max(0.12, min(1.6, (dist / 1600.0) / max(float(self.cfg.get("speed_multiplier", 1.0)), 0.25)))
        if random.random() < float(self.cfg.get("overshoot_rate", 0.15)):
            angle = math.atan2(ty - sy, tx - sx)
            over = random.uniform(6.0, 22.0)
            ox, oy = self._clamp(tx + math.cos(angle) * over, ty + math.sin(angle) * over)
            self._move_bezier(sx, sy, ox, oy, duration)
            self._move_bezier(ox, oy, tx, ty, duration * 0.35)
        else:
            self._move_bezier(sx, sy, tx, ty, duration)
        if random.random() < float(self.cfg.get("hesitation_before_click", 0.45)):
            self._micro_hesitate()

    def _handle_typo(self, target: str) -> None:
        if ord(target[:1] or "\0") > 127:
            wrong = random.choice(KOREAN_TYPO_POOL)
        else:
            wrong = random.choice(QWERTY_NEIGHBORS.get(target.lower(), target))
        if target.isupper():
            wrong = wrong.upper()
        self.page.keyboard.type(wrong)
        self.random_action_delay("오타 후 멈칫", 0.06, 0.22)
        self.page.keyboard.press("Backspace")
        self.random_action_delay("오타 수정 후 딜레이", 0.04, 0.16)
        self._log_action(f"오타 시뮬레이션: '{wrong}' -> 백스페이스")

    def _jitter_mouse_during_typing(self) -> None:
        if self.page is None or random.random() > 0.12:
            return
        self.mouse_x, self.mouse_y = self._clamp(self.mouse_x + random.uniform(-1.5, 1.5), self.mouse_y + random.uniform(-1.5, 1.5))
        self.page.mouse.move(self.mouse_x, self.mouse_y)
        time.sleep(random.uniform(0.01, 0.04))

    def _move_bezier(self, x1: float, y1: float, x2: float, y2: float, duration: float) -> None:
        if self.page is None:
            return
        steps = max(12, int(duration * random.randint(55, 95)))
        dist = math.hypot(x2 - x1, y2 - y1)
        distortion = max(8.0, dist * random.uniform(0.08, 0.18))
        cp1 = (x1 + (x2 - x1) * random.uniform(0.2, 0.4) + random.uniform(-distortion, distortion), y1 + (y2 - y1) * random.uniform(0.2, 0.4) + random.uniform(-distortion, distortion))
        cp2 = (x1 + (x2 - x1) * random.uniform(0.6, 0.8) + random.uniform(-distortion, distortion), y1 + (y2 - y1) * random.uniform(0.6, 0.8) + random.uniform(-distortion, distortion))
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3.0 - 2.0 * t)
            px, py = self._bezier((x1, y1), cp1, cp2, (x2, y2), eased)
            if random.random() < 0.25:
                px += random.uniform(-1.0, 1.0)
                py += random.uniform(-1.0, 1.0)
            px, py = self._clamp(px, py)
            self.page.mouse.move(px, py)
            time.sleep(max(0.001, duration / steps * random.uniform(0.6, 1.45)))
        self.mouse_x, self.mouse_y = x2, y2

    def _micro_hesitate(self) -> None:
        if self.page is None:
            time.sleep(random.uniform(0.04, 0.18))
            return
        for _ in range(random.randint(1, 3)):
            self.mouse_x, self.mouse_y = self._clamp(self.mouse_x + random.uniform(-1.6, 1.6), self.mouse_y + random.uniform(-1.6, 1.6))
            self.page.mouse.move(self.mouse_x, self.mouse_y)
            time.sleep(random.uniform(0.02, 0.07))

    def _viewport(self) -> tuple[int, int]:
        if self.page is None:
            return self._viewport_cache
        try:
            width, height = self.page.evaluate("() => [window.innerWidth, window.innerHeight]")
            self._viewport_cache = (int(width), int(height))
        except Exception:
            pass
        return self._viewport_cache

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        width, height = self._viewport()
        return max(2.0, min(float(x), width - 2.0)), max(2.0, min(float(y), height - 2.0))

    @staticmethod
    def _bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
        one = 1.0 - t
        return (
            one ** 3 * p0[0] + 3 * one ** 2 * t * p1[0] + 3 * one * t ** 2 * p2[0] + t ** 3 * p3[0],
            one ** 3 * p0[1] + 3 * one ** 2 * t * p1[1] + 3 * one * t ** 2 * p2[1] + t ** 3 * p3[1],
        )

    def _log_action(self, message: str) -> None:
        if not self.action_logger:
            return
        self.action_logger(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
