"""Плавающий виджет лимитов Claude (Windows/macOS/Linux). Только стандартная библиотека.

Левая кнопка: перетащить; короткий клик переключает «через сколько» и «во сколько» сброс.
Правая кнопка: меню (источник, вид, размер, тема, компактный режим и т. д.).
"""
import json
import math
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import limits_core as core

CONFIG = Path.home() / ".claude-limits-widget.json"
DEFAULTS = {
    "mode": "file", "x": 40, "y": 40, "alpha": 0.92, "topmost": True, "live_ok": False,
    "hidden": [], "shown_extra": [], "show_breakdown": True,
    "view": "bars", "size": "M", "theme": "auto", "accent": "Зелёный",
    "compact": False, "pulse": True, "snap": True, "ghost": False,
    "reset_abs": False, "rounded": False, "auto_login": True, "pace": True,
}

PALETTES = {
    "dark": {"bg": "#1f1f1f", "border": "#3a3a3a", "track": "#363636", "fg": "#ececec", "dim": "#9a9a9a"},
    "light": {"bg": "#f7f7f7", "border": "#cfcfcf", "track": "#e0e0e0", "fg": "#1b1b1b", "dim": "#6a6a6a"},
}
ACCENTS = {"Зелёный": "#30a46c", "Синий": "#3b82f6", "Фиолетовый": "#8b5cf6", "Бирюзовый": "#14b8a6"}
SIZES = {"S": 0.85, "M": 1.0, "L": 1.25}
KEYCOLOR = "#010203"  # прозрачный цвет для скруглённых углов (только Windows)
STALE_SECONDS = 300
AUTO_LOGIN_COOLDOWN, AUTO_LOGIN_MAX_FAILS = 1800, 3
FILE_POLL_MS, LIVE_POLL_MS, REDRAW_MS, PULSE_MS = 5_000, 120_000, 30_000, 700
GHOST_ALPHA = 0.35
RED, AMBER = "#e5484d", "#f5a524"


def load_config() -> dict:
    try:
        cfg = {**DEFAULTS, **json.loads(CONFIG.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        cfg = dict(DEFAULTS)
    if cfg["mode"] not in ("file", "live"):
        cfg["mode"] = "file"
    if cfg["view"] not in ("bars", "rings"):
        cfg["view"] = "bars"
    if cfg["size"] not in SIZES:
        cfg["size"] = "M"
    if cfg["theme"] not in ("auto", "dark", "light"):
        cfg["theme"] = "auto"
    if cfg["accent"] not in ACCENTS:
        cfg["accent"] = "Зелёный"
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def enable_dpi_awareness() -> None:
    """Без этого на экранах с масштабом выше 100% Windows растягивает окно и оно мылится."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def system_theme() -> str:
    """«light» или «dark» по настройке Windows; где её нет, тёмная."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "dark"


def monitor_rects() -> list:
    """Рабочие области всех мониторов [(left, top, right, bottom)] в пикселях экрана; вне Windows пусто."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        rects: list = []
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

        def callback(hmon, _hdc, _rect, _lparam):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(info)):
                r = info.rcWork
                rects.append((r.left, r.top, r.right, r.bottom))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
        return rects
    except Exception:
        return []


def pick_monitor(rects: list, cx: int, cy: int) -> tuple:
    """Монитор, на котором лежит точка; если ни на одном, то ближайший."""
    for r in rects:
        if r[0] <= cx < r[2] and r[1] <= cy < r[3]:
            return r

    def dist(r) -> int:
        dx = max(r[0] - cx, 0, cx - r[2] + 1)
        dy = max(r[1] - cy, 0, cy - r[3] + 1)
        return dx * dx + dy * dy

    return min(rects, key=dist)


def snap_position(x: int, y: int, w: int, h: int, rect: tuple, margin: int, thresh: int) -> tuple:
    """Прижимает окно к краю прямоугольника монитора, если оно ближе thresh пикселей."""
    left, top, right, bottom = rect
    if x - left < thresh:
        x = left + margin
    elif right - (x + w) < thresh:
        x = right - w - margin
    if y - top < thresh:
        y = top + margin
    elif bottom - (y + h) < thresh:
        y = bottom - h - margin
    return x, y


def clamp_into(x: int, y: int, w: int, h: int, rects: list) -> tuple:
    """Оставляет окно где есть, если его центр на каком-то мониторе; иначе переносит на ближайший."""
    cx, cy = x + w // 2, y + h // 2
    for r in rects:
        if r[0] <= cx < r[2] and r[1] <= cy < r[3]:
            return x, y
    r = pick_monitor(rects, cx, cy)
    return min(max(x, r[0]), max(r[2] - w, r[0])), min(max(y, r[1]), max(r[3] - h, r[1]))


class Widget:
    def __init__(self) -> None:
        enable_dpi_awareness()
        self.cfg = load_config()
        self.data = core.Data(error="Загрузка…")
        self.results: "queue.Queue[core.Data]" = queue.Queue()
        self.busy = False
        self.next_live_at = 0.0
        self.backoff = 0
        self.hovering = False
        self.menu_open = False
        self.pulse_on = False
        self._collapse_job = None
        self._press = None
        self._moved = False
        self._vars: dict = {}
        self._buttons: list = []  # (x1, y1, x2, y2, действие) кнопок, нарисованных на холсте
        self.login_busy = False
        self.login_msg = ""
        self.login_msg_at = 0.0
        self.login_fails = 0
        self.last_login_try = 0.0
        self.login_results: "queue.Queue[tuple]" = queue.Queue()
        self._filter_vars: dict = {}
        self.theme_name = "dark"
        self.pal = PALETTES["dark"]

        self.root = tk.Tk()
        self.root.title("Лимиты Claude")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(self.cfg["topmost"]))
        self.dpi = max(self.root.winfo_fpixels("1i") / 96.0, 0.5)
        self.k = self.dpi

        self.canvas = tk.Canvas(self.root, width=10, height=10, highlightthickness=0, bd=0)
        self.canvas.pack(padx=1, pady=1)
        self.canvas.bind("<ButtonPress-1>", self._press_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._press_end)
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", self._leave)
        for ev in ("<Button-3>", "<Control-Button-1>"):
            self.canvas.bind(ev, self._menu)

        self._build_menu()
        self._resolve_theme()
        self._apply_chrome()
        self._apply_alpha()
        self.render()
        self.root.update_idletasks()
        self._place()
        self.refresh()
        self.root.after(200, self._poll_results)
        self.root.after(REDRAW_MS, self._redraw_loop)
        self.root.after(FILE_POLL_MS, self._auto_refresh)
        self.root.after(PULSE_MS, self._pulse_loop)

    # ---------- размеры и шрифты ----------
    def px(self, n: float) -> int:
        return max(1, int(round(n * self.k)))

    def font(self, pt: float, bold: bool = False):
        size = max(6, int(round(pt * SIZES[self.cfg["size"]])))
        return ("Segoe UI", size, "bold") if bold else ("Segoe UI", size)

    # ---------- меню ----------
    def _radio_menu(self, parent, title, key, options, command=None) -> None:
        var = tk.StringVar(value=self._radio_value(key))
        self._vars[key] = var
        sub = tk.Menu(parent, tearoff=0)
        for label, value in options:
            sub.add_radiobutton(label=label, value=value, variable=var,
                                command=command or (lambda k=key: self._radio_changed(k)))
        parent.add_cascade(label=title, menu=sub)

    def _radio_value(self, key: str) -> str:
        if key == "alpha":
            return str(round(float(self.cfg["alpha"]) * 100))
        return str(self.cfg[key])

    def _check(self, parent, label: str, key: str) -> None:
        var = tk.BooleanVar(value=bool(self.cfg[key]))
        self._vars[key] = var
        parent.add_checkbutton(label=label, variable=var, command=lambda k=key: self._toggled(k))

    def _build_menu(self) -> None:
        m = self.menu = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Обновить сейчас", command=self.refresh)
        m.add_command(label="Обновить вход (запустит claude)", command=lambda: self.start_login(manual=True))
        self._radio_menu(m, "Источник данных", "mode",
                         [("Из файла (безопасно)", "file"), ("Live (токен Claude Code)", "live")],
                         self._mode_changed)
        self._radio_menu(m, "Вид", "view", [("Шкалы", "bars"), ("Кольца", "rings")])
        self._radio_menu(m, "Размер", "size", [("Мелкий", "S"), ("Средний", "M"), ("Крупный", "L")])
        self._radio_menu(m, "Тема", "theme",
                         [("Авто (как в Windows)", "auto"), ("Тёмная", "dark"), ("Светлая", "light")])
        self._radio_menu(m, "Цвет шкал", "accent", [(n, n) for n in ACCENTS])
        self._radio_menu(m, "Прозрачность", "alpha",
                         [("100%", "100"), ("90%", "90"), ("75%", "75"), ("60%", "60")])
        m.add_separator()
        self._check(m, "Компактный режим (разворачивается при наведении)", "compact")
        self._check(m, "Время сброса: точное («в 14:40»)", "reset_abs")
        self._check(m, "Неделя по продуктам", "show_breakdown")
        self._check(m, "Темп и прогноз исчерпания", "pace")
        self._check(m, "Пульсация при 90% и выше", "pulse")
        self._check(m, "Автоматически обновлять вход в Live", "auto_login")
        m.add_separator()
        self._check(m, "Поверх всех окон", "topmost")
        self._check(m, "Прилипать к краям экрана", "snap")
        self._check(m, "Бледнеть, когда мышь не рядом", "ghost")
        self._check(m, "Скруглённые углы (экспериментально)", "rounded")
        m.add_separator()
        self.filter_menu = tk.Menu(m, tearoff=0)
        m.add_cascade(label="Показывать лимиты", menu=self.filter_menu)
        m.add_command(label="Сырой ответ…", command=self.show_raw)
        m.add_separator()
        m.add_command(label="Выход", command=self.root.destroy)

    def _menu(self, e) -> None:
        self._rebuild_filter_menu()
        self.menu_open = True
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()
            self.menu_open = False

    def _rebuild_filter_menu(self) -> None:
        self.filter_menu.delete(0, "end")
        self._filter_vars = {}
        if not self.data.windows:
            self.filter_menu.add_command(label="(нет данных)", state="disabled")
            return
        for w in self.data.windows:
            var = tk.BooleanVar(value=self._is_visible(w.kind))
            self._filter_vars[w.kind] = var
            self.filter_menu.add_checkbutton(label=core.label_for(w.kind), variable=var,
                                             command=self._filter_changed)

    def _is_visible(self, kind: str) -> bool:
        if kind in core.KNOWN:
            return kind not in self.cfg["hidden"]
        return kind in self.cfg["shown_extra"]  # незнакомые окна скрыты, пока не включишь

    def _filter_changed(self) -> None:
        self.cfg["hidden"] = [k for k, v in self._filter_vars.items() if k in core.KNOWN and not v.get()]
        self.cfg["shown_extra"] = [k for k, v in self._filter_vars.items() if k not in core.KNOWN and v.get()]
        save_config(self.cfg)
        self.render()

    def _toggled(self, key: str) -> None:
        self.cfg[key] = bool(self._vars[key].get())
        save_config(self.cfg)
        if key == "topmost":
            self.root.attributes("-topmost", self.cfg["topmost"])
        elif key == "rounded":
            self._apply_chrome()
        elif key == "ghost":
            self._apply_alpha()
        self.render()

    def _radio_changed(self, key: str) -> None:
        value = self._vars[key].get()
        self.cfg[key] = int(value) / 100 if key == "alpha" else value
        save_config(self.cfg)
        if key == "theme":
            self._resolve_theme()
            self._apply_chrome()
        elif key == "alpha":
            self._apply_alpha()
        self.render()

    def show_raw(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Сырой ответ (токен не включён)")
        win.attributes("-topmost", True)
        text = tk.Text(win, width=70, height=24, font=("Consolas", 9), wrap="none")
        text.pack(fill="both", expand=True)
        body = json.dumps(self.data.raw, indent=2, ensure_ascii=False) if self.data.raw is not None \
            else "Данных пока нет."
        text.insert("1.0", body)

        def copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(body)

        tk.Button(win, text="Копировать", command=copy).pack(pady=4)

    # ---------- окно ----------
    def _resolve_theme(self) -> bool:
        name = system_theme() if self.cfg["theme"] == "auto" else self.cfg["theme"]
        changed = name != self.theme_name
        self.theme_name, self.pal = name, PALETTES.get(name, PALETTES["dark"])
        return changed

    def _apply_chrome(self) -> None:
        pal = self.pal
        if self.cfg["rounded"]:
            self.root.configure(bg=KEYCOLOR)
            self.canvas.configure(bg=KEYCOLOR)
            self.canvas.pack_configure(padx=0, pady=0)
            try:
                self.root.attributes("-transparentcolor", KEYCOLOR)
            except tk.TclError:
                pass  # не Windows: углы остаются прямыми
        else:
            try:
                self.root.attributes("-transparentcolor", "")
            except tk.TclError:
                pass
            self.root.configure(bg=pal["border"])
            self.canvas.configure(bg=pal["bg"])
            self.canvas.pack_configure(padx=1, pady=1)

    def _apply_alpha(self) -> None:
        alpha = float(self.cfg["alpha"])
        if self.cfg["ghost"] and not self.hovering:
            alpha = min(alpha, GHOST_ALPHA)
        try:
            self.root.attributes("-alpha", alpha)
        except tk.TclError:
            pass

    def _monitors(self) -> list:
        rects = monitor_rects()
        return rects or [(0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())]

    def _place(self) -> None:
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x, y = clamp_into(int(self.cfg["x"]), int(self.cfg["y"]), w, h, self._monitors())
        self.root.geometry(f"+{x}+{y}")

    def _snap(self) -> tuple:
        """Прилипает к краям того монитора, на котором стоит окно. Возвращает итоговые (x, y)."""
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if not self.cfg["snap"]:
            return x, y
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        rect = pick_monitor(self._monitors(), x + w // 2, y + h // 2)
        nx, ny = snap_position(x, y, w, h, rect, self.px(8), self.px(22))
        if (nx, ny) != (x, y):
            self.root.geometry(f"+{nx}+{ny}")
        return nx, ny

    def _press_start(self, e) -> None:
        self._press = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def _drag_move(self, e) -> None:
        if not self._press:
            return
        px0, py0, wx, wy = self._press
        dx, dy = e.x_root - px0, e.y_root - py0
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
        if self._moved:
            self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _press_end(self, _e) -> None:
        if self._moved:
            self.cfg["x"], self.cfg["y"] = self._snap()
            save_config(self.cfg)
        elif self._hit_button(_e) is not None:
            self._hit_button(_e)()
        else:  # короткий клик: «через сколько» ↔ «во сколько»
            self.cfg["reset_abs"] = not self.cfg["reset_abs"]
            if "reset_abs" in self._vars:
                self._vars["reset_abs"].set(self.cfg["reset_abs"])
            save_config(self.cfg)
            self.render()
        self._press = None

    def _hit_button(self, e):
        x, y = getattr(e, "x", -1), getattr(e, "y", -1)
        for x1, y1, x2, y2, action in self._buttons:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return action
        return None

    def _enter(self, _e) -> None:
        if self._collapse_job is not None:
            self.root.after_cancel(self._collapse_job)
            self._collapse_job = None
        if not self.hovering:
            self.hovering = True
            self._apply_alpha()
            if self.cfg["compact"]:
                self.render()

    def _leave(self, _e) -> None:
        if self._collapse_job is None:
            self._collapse_job = self.root.after(500, self._collapse)

    def _collapse(self) -> None:
        self._collapse_job = None
        if self.menu_open:
            self._collapse_job = self.root.after(500, self._collapse)
            return
        self.hovering = False
        self._apply_alpha()
        if self.cfg["compact"]:
            self.render()

    # ---------- данные ----------
    def _mode_changed(self) -> None:
        mode = self._vars["mode"].get()
        if mode == "live" and not self.cfg["live_ok"]:
            ok = messagebox.askokcancel(
                "Режим Live",
                "Виджет будет сам обращаться к недокументированному адресу Anthropic, "
                "используя твой токен входа Claude Code из файла .credentials.json.\n\n"
                "Это неофициальный способ: он может нарушать условия использования, "
                "перестать работать или получать ошибки 429. Токен никуда, кроме api.anthropic.com, "
                "не отправляется.\n\nВключить?")
            if not ok:
                self._vars["mode"].set("file")
                return
            self.cfg["live_ok"] = True
        self.cfg["mode"] = mode
        save_config(self.cfg)
        self.backoff = 0
        self.next_live_at = 0.0
        self.refresh()

    def refresh(self) -> None:
        if self.busy:
            return
        self.busy = True
        mode = self.cfg["mode"]

        def work() -> None:
            try:
                res = core.fetch_live() if mode == "live" else core.read_snapshot()
            except Exception as exc:  # виджет не должен падать
                res = core.Data(error=f"Ошибка: {exc}")
            self.results.put(res)

        threading.Thread(target=work, daemon=True).start()

    def _poll_results(self) -> None:
        try:
            while True:
                res = self.results.get_nowait()
                self.busy = False
                if self.cfg["mode"] == "live":
                    if res.error and res.error.startswith("429"):
                        self.backoff = min(self.backoff + 1, 4)
                    elif not res.error:
                        self.backoff = 0
                    self.next_live_at = time.time() + LIVE_POLL_MS / 1000 * (2 ** self.backoff)
                # при ошибке оставляем прошлые цифры, но показываем причину
                if res.error and self.data.windows:
                    res = core.Data(self.data.windows, self.data.context_percent,
                                    self.data.updated_at, res.error, self.data.raw, self.data.breakdown,
                                    res.auth_error)
                self.data = res
                self._maybe_auto_login()
                self.render()
        except queue.Empty:
            pass
        try:
            while True:
                self._login_done(*self.login_results.get_nowait())
        except queue.Empty:
            pass
        self.root.after(200, self._poll_results)

    # ---------- обновление входа ----------
    def _maybe_auto_login(self) -> None:
        if (self.cfg["mode"] == "live" and self.cfg["auto_login"] and self.data.auth_error
                and not self.login_busy and self.login_fails < AUTO_LOGIN_MAX_FAILS
                and time.time() - self.last_login_try > AUTO_LOGIN_COOLDOWN):
            self.start_login(manual=False)

    def start_login(self, manual: bool = False) -> None:
        if self.login_busy:
            return
        self.login_busy = True
        self.last_login_try = time.time()
        if manual:
            self.login_fails = 0
        self._set_login_msg("Обновляю вход…")
        self.render()

        def work() -> None:
            try:
                result = core.refresh_login()
            except Exception as exc:
                result = (False, f"Ошибка: {exc}")
            self.login_results.put(result)

        threading.Thread(target=work, daemon=True).start()

    def _login_done(self, ok: bool, message: str) -> None:
        self.login_busy = False
        self._set_login_msg(message)
        if ok:
            self.login_fails = 0
            self.backoff = 0
            self.next_live_at = 0.0
            self.refresh()
        else:
            self.login_fails += 1
        self.render()

    def _set_login_msg(self, text: str) -> None:
        self.login_msg, self.login_msg_at = text, time.time()

    def _auto_refresh(self) -> None:
        if self.cfg["mode"] == "file" or time.time() >= self.next_live_at:
            self.refresh()
        self.root.after(FILE_POLL_MS, self._auto_refresh)

    def _redraw_loop(self) -> None:
        if self._resolve_theme():
            self._apply_chrome()
        self.render()
        self.root.after(REDRAW_MS, self._redraw_loop)

    def _pulse_loop(self) -> None:
        self.pulse_on = not self.pulse_on
        if self.cfg["pulse"] and any(w.percent >= 90 for w in self._visible()):
            self.render()
        self.root.after(PULSE_MS, self._pulse_loop)

    # ---------- рисование ----------
    def _visible(self) -> list:
        return [w for w in self.data.windows if self._is_visible(w.kind)]

    def _is_stale(self, now: float) -> bool:
        d = self.data
        return bool(d.error) or (d.updated_at is not None and now - d.updated_at > STALE_SECONDS)

    def _tint(self, color: str) -> str:
        return core.mix(color, self.pal["bg"], 0.5) if self._stale else color

    def _fill_color(self, w) -> str:
        col = core.color_for(max(w.percent, 0.0), w.severity, ACCENTS[self.cfg["accent"]])
        if self.cfg["pulse"] and self.pulse_on and w.percent >= 90:
            col = core.mix(col, "#ffffff", 0.35)
        return self._tint(col)

    def _reset_text(self, w, now: float) -> str:
        if self.cfg["reset_abs"]:
            return core.format_reset_abs(w.resets_at, now)
        return core.format_reset(w.resets_at, now)

    def _forecast_text(self, w, now: float) -> str:
        if not self.cfg["pace"] or w.percent >= 100:
            return ""
        at = core.forecast_exhaust(w, now)
        return f"При таком темпе кончится {core.format_reset_abs(at, now)}" if at else ""

    def _value_text(self, w) -> str:
        if w.limit_dollars is not None and w.used_dollars is not None:
            return f"${w.used_dollars:.0f} / ${w.limit_dollars:.0f}"
        return f"{max(w.percent, 0):.0f}%"

    def _pace(self, w, now: float):
        return core.pace_percent(w, now) if self.cfg["pace"] else None

    def _bar(self, x1, y, x2, height, pct, color, pace=None) -> None:
        c, pal = self.canvas, self.pal
        c.create_rectangle(x1, y, x2, y + height, fill=pal["track"], outline="")
        fill_w = (x2 - x1) * min(max(pct, 0), 100) / 100
        if fill_w > 0:
            c.create_rectangle(x1, y, x1 + fill_w, y + height, fill=color, outline="")
        if height >= self.px(5):  # отметки 70% и 90%
            for t in (70, 90):
                tx = x1 + (x2 - x1) * t / 100
                c.create_line(tx, y, tx, y + height, fill=pal["bg"])
        if pace is not None:  # где была бы шкала при ровном расходе
            tx = x1 + (x2 - x1) * min(max(pace, 0), 100) / 100
            c.create_line(tx, y - self.px(3), tx, y + height + self.px(3), fill=self._tint(pal["fg"]),
                          width=max(self.px(2), 1))

    def render(self) -> None:
        cfg, d, pal, c = self.cfg, self.data, self.pal, self.canvas
        now = time.time()
        self.k = self.dpi * SIZES[cfg["size"]]
        self._stale = self._is_stale(now)
        px = self.px
        self._W, self._PAD = px(270), px(12)
        c.delete("all")
        self._buttons = []
        visible = self._visible()
        compact = cfg["compact"] and not self.hovering

        y = self._PAD
        if compact:
            y = self._draw_compact(visible, y, now)
        else:
            y = self._draw_header(y)
            y = self._draw_rings(visible, y, now) if cfg["view"] == "rings" else self._draw_bars(visible, y, now)
            y = self._draw_breakdown(y)
            y = self._draw_context(y)
            y = self._draw_note(y, now)
        height = max(y + self._PAD // 2, px(40))

        # фон рисуем последним и опускаем под всё остальное
        if cfg["rounded"]:
            self._round_rect(0, 0, self._W - 1, height - 1, px(12), fill=pal["bg"], outline=pal["border"])
        else:
            c.create_rectangle(0, 0, self._W, height, fill=pal["bg"], outline="", tags="bgfill")
        c.tag_lower("bgfill") if not cfg["rounded"] else c.tag_lower("roundbg")
        c.configure(width=self._W, height=height)

    def _round_rect(self, x1, y1, x2, y2, r, **kw) -> None:
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        self.canvas.create_polygon(pts, smooth=True, tags="roundbg", **kw)

    def _freshness_color(self) -> str:
        if self.data.error:
            return RED
        return AMBER if self._stale else ACCENTS[self.cfg["accent"]]

    def _draw_header(self, y: int) -> int:
        c, pal, px, W, PAD = self.canvas, self.pal, self.px, self._W, self._PAD
        c.create_text(PAD, y, anchor="nw", text="Claude · лимиты", fill=pal["dim"], font=self.font(8, True))
        r = px(4)
        cx, cy = W - PAD - r, y + px(7)
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=self._freshness_color(), outline="")
        return y + px(22)

    def _draw_bars(self, visible, y: int, now: float) -> int:
        c, pal, px, W, PAD = self.canvas, self.pal, self.px, self._W, self._PAD
        for w in visible:
            col = self._fill_color(w)
            c.create_text(PAD, y, anchor="nw", text=core.label_for(w.kind),
                          fill=self._tint(pal["fg"]), font=self.font(9))
            right = self._value_text(w)
            reset = self._reset_text(w, now)
            if reset:
                right += f" · {reset}"
            c.create_text(W - PAD, y, anchor="ne", text=right, fill=col, font=self.font(9, True))
            self._bar(PAD, y + px(20), W - PAD, px(6), w.percent, col, self._pace(w, now))
            y += px(38)
            warn = self._forecast_text(w, now)
            if warn:
                c.create_text(PAD, y - px(10), anchor="nw", text=warn, fill=self._tint(AMBER), font=self.font(8))
                y += px(14)
        return y

    def _draw_rings(self, visible, y: int, now: float) -> int:
        c, pal, px, W, PAD = self.canvas, self.pal, self.px, self._W, self._PAD
        shown = visible[:4]
        if not shown:
            return y
        colw = (W - 2 * PAD) / len(shown)
        diam = int(min(px(78), colw - px(10)))
        thick = max(px(7), 3)
        for i, w in enumerate(shown):
            col = self._fill_color(w)
            cx = PAD + colw * (i + 0.5)
            x1, y1, x2, y2 = cx - diam / 2, y + thick, cx + diam / 2, y + thick + diam
            c.create_oval(x1, y1, x2, y2, outline=pal["track"], width=thick)
            extent = -min(max(w.percent, 0), 99.9) * 3.6
            if extent < 0:
                c.create_arc(x1, y1, x2, y2, start=90, extent=extent, style="arc", outline=col, width=thick)
            pace = self._pace(w, now)
            if pace is not None:  # метка ровного темпа поперёк кольца
                a = math.radians(90 - min(max(pace, 0), 100) * 3.6)
                rin, rout = diam / 2 - thick / 2 - px(1), diam / 2 + thick / 2 + px(1)
                cy = (y1 + y2) / 2
                c.create_line(cx + rin * math.cos(a), cy - rin * math.sin(a),
                              cx + rout * math.cos(a), cy - rout * math.sin(a),
                              fill=self._tint(pal["fg"]), width=max(px(2), 1))
            center = f"${w.used_dollars:.0f}" if w.used_dollars is not None and w.limit_dollars is not None \
                else f"{max(w.percent, 0):.0f}%"
            c.create_text(cx, (y1 + y2) / 2, text=center, fill=col, font=self.font(11, True))
            c.create_text(cx, y2 + thick + px(2), anchor="n", text=core.short_label(w.kind),
                          fill=self._tint(pal["fg"]), font=self.font(8))
            reset = self._reset_text(w, now)
            if reset:
                c.create_text(cx, y2 + thick + px(16), anchor="n", text=reset, fill=pal["dim"], font=self.font(8))
        return y + thick + diam + thick + px(34)

    def _draw_compact(self, visible, y: int, now: float) -> int:
        c, pal, px, W, PAD = self.canvas, self.pal, self.px, self._W, self._PAD
        shown = visible[:2]
        if not shown:
            c.create_text(PAD, y, anchor="nw", text="Лимиты: нет данных", fill=pal["dim"], font=self.font(8))
            return y + px(16)
        colw = (W - 2 * PAD) / len(shown)
        for i, w in enumerate(shown):
            col = self._fill_color(w)
            x1 = PAD + colw * i
            x2 = x1 + colw - (px(8) if i < len(shown) - 1 else 0)
            c.create_text(x1, y, anchor="nw", text=f"{core.short_label(w.kind)} {self._value_text(w)}",
                          fill=col, font=self.font(9, True))
            self._bar(x1, y + px(18), x2, px(4), w.percent, col)
        r = px(3)
        c.create_oval(W - PAD - r * 2, y + px(2), W - PAD, y + px(2) + r * 2,
                      fill=self._freshness_color(), outline="")
        return y + px(28)

    def _draw_breakdown(self, y: int) -> int:
        d, c, pal, px, W, PAD = self.data, self.canvas, self.pal, self.px, self._W, self._PAD
        total = sum(max(r.percent, 0) for r in d.breakdown)
        if not (self.cfg["show_breakdown"] and total > 0):
            return y
        inner = W - 2 * PAD
        c.create_text(PAD, y - px(6), anchor="nw", text="Неделя по продуктам", fill=pal["dim"], font=self.font(8))
        by, x = y + px(10), PAD
        for r in d.breakdown:
            if r.percent <= 0:
                continue
            seg = inner * r.percent / total
            c.create_rectangle(x, by, x + seg, by + px(6), outline="",
                               fill=self._tint(core.BREAKDOWN_COLORS.get(r.key, "#8a8a8a")))
            x += seg
        legend = " · ".join(f"{r.name} {r.percent:.0f}%" for r in d.breakdown if r.percent > 0)
        c.create_text(PAD, by + px(9), anchor="nw", width=inner, text=legend,
                      fill=self._tint(pal["fg"]), font=self.font(8))
        return y + px(46)

    def _draw_context(self, y: int) -> int:
        d, c, pal, px, PAD = self.data, self.canvas, self.pal, self.px, self._PAD
        if d.context_percent is None:
            return y
        c.create_text(PAD, y - px(6), anchor="nw", text=f"Контекст сессии {d.context_percent:.0f}%",
                      fill=pal["dim"], font=self.font(8))
        return y + px(16)

    def _draw_note(self, y: int, now: float) -> int:
        d, c, pal, px, PAD, W = self.data, self.canvas, self.pal, self.px, self._PAD, self._W
        note = d.error
        if not note and self.cfg["mode"] == "file" and d.updated_at and now - d.updated_at > STALE_SECONDS:
            note = f"Данные от {time.strftime('%H:%M', time.localtime(d.updated_at))}"
        if note:
            c.create_text(PAD, y - px(4), anchor="nw", width=W - 2 * PAD, text=note,
                          fill=AMBER if d.error else pal["dim"], font=self.font(8))
            y += px(14) * (1 + len(note) // 38)
        if d.auth_error and self.cfg["mode"] == "live":
            y = self._draw_login_button(y)
        elif self.login_msg and now - self.login_msg_at < 60:
            c.create_text(PAD, y - px(2), anchor="nw", width=W - 2 * PAD, text=self.login_msg,
                          fill=pal["dim"], font=self.font(8))
            y += px(14)
        return y

    def _draw_login_button(self, y: int) -> int:
        c, pal, px, PAD, W = self.canvas, self.pal, self.px, self._PAD, self._W
        accent = ACCENTS[self.cfg["accent"]]
        label = "Обновляю вход…" if self.login_busy else "Обновить вход"
        x1, y1, x2, y2 = PAD, y + px(2), W - PAD, y + px(28)
        c.create_rectangle(x1, y1, x2, y2, outline=pal["dim"] if self.login_busy else accent, width=1,
                           fill=pal["track"])
        c.create_text((x1 + x2) / 2, (y1 + y2) / 2, text=label, font=self.font(9, True),
                      fill=pal["dim"] if self.login_busy else accent)
        if not self.login_busy:
            self._buttons.append((x1, y1, x2, y2, lambda: self.start_login(manual=True)))
        y = y2 + px(4)
        recent = self.login_msg and time.time() - self.login_msg_at < 60 and not self.login_busy
        if recent:
            c.create_text(PAD, y, anchor="nw", width=W - 2 * PAD, text=self.login_msg,
                          fill=pal["dim"], font=self.font(8))
            y += px(14) * (1 + len(self.login_msg) // 38)
        return y

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    Widget().run()
