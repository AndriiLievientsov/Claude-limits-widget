"""Дымовой тест окна на поддельном tkinter: ловит опечатки и ошибки логики, но не проверяет вид."""
import itertools
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import limits_core as core


class Dummy:
    """Принимает любые вызовы; create_* считает."""
    def __init__(self, *a, **k):
        self.calls = []
        self._id = itertools.count(1)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*a, **k):
            self.calls.append((name, a, k))
            if name == "winfo_fpixels":
                return 144.0  # 150% масштаб
            if name in ("winfo_reqwidth", "winfo_width"):
                return 270
            if name in ("winfo_reqheight", "winfo_height"):
                return 150
            if name in ("winfo_screenwidth",):
                return 1920
            if name in ("winfo_screenheight",):
                return 1080
            if name in ("winfo_x", "winfo_y"):
                return 10
            if name == "after":
                return "job"
            return next(self._id)
        return call


class Var:
    def __init__(self, value=None):
        self.v = value

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


fake = types.ModuleType("tkinter")
for cls in ("Tk", "Canvas", "Menu", "Toplevel", "Text", "Button"):
    setattr(fake, cls, Dummy)
fake.StringVar, fake.BooleanVar = Var, Var
fake.TclError = type("TclError", (Exception,), {})
msg = types.ModuleType("tkinter.messagebox")
msg.askokcancel = lambda *a, **k: True
fake.messagebox = msg
sys.modules["tkinter"], sys.modules["tkinter.messagebox"] = fake, msg

import limits_widget as lw  # noqa: E402

NOW = time.time()
DATA = core.Data(
    [core.Window("five_hour", 57, NOW + 3 * 3600), core.Window("seven_day", 95, NOW + 6 * 86400, "normal"),
     core.Window("iguana_necktie", 100, NOW + 24 * 86400, None, 100.28, 100.0)],
    context_percent=18, updated_at=NOW,
    breakdown=[core.Share("claude_code", "Code", 17), core.Share("cowork", "Cowork", 83)])


class Smoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        lw.CONFIG = Path(self.tmp.name) / "cfg.json"
        core.SNAPSHOT = Path(self.tmp.name) / "none.json"
        self.w = lw.Widget()

    def tearDown(self):
        self.tmp.cleanup()

    def texts(self):
        return [k.get("text", "") for n, a, k in self.w.canvas.calls if n == "create_text"]

    def test_all_option_combinations_render(self):
        self.w.cfg["shown_extra"] = ["iguana_necktie"]
        for view, size, theme, compact, hover, rounded, stale in itertools.product(
                ("bars", "rings"), ("S", "M", "L"), ("dark", "light"), (False, True), (False, True),
                (False, True), (False, True)):
            self.w.cfg.update(view=view, size=size, theme=theme, compact=compact, rounded=rounded)
            self.w.hovering = hover
            self.w.data = DATA if not stale else core.Data(DATA.windows, 18, NOW - 999, "Нет связи",
                                                           breakdown=DATA.breakdown)
            self.w.pulse_on = stale
            self.w._resolve_theme()
            self.w.canvas.calls.clear()
            self.w.render()
            self.assertTrue(self.w.canvas.calls, (view, size, theme, compact, hover, rounded))

    def test_empty_and_error_states(self):
        for d in (core.Data(), core.Data(error="x"), core.Data([], None, None, "нет данных")):
            for view in ("bars", "rings"):
                for compact in (False, True):
                    self.w.cfg.update(view=view, compact=compact)
                    self.w.data = d
                    self.w.render()

    def test_bars_show_values_and_reset(self):
        self.w.cfg.update(view="bars", compact=False, reset_abs=False)
        self.w.data = DATA
        self.w.canvas.calls.clear()
        self.w.render()
        joined = " | ".join(self.texts())
        self.assertIn("57%", joined)
        self.assertIn("Неделя по продуктам", joined)
        self.assertNotIn("$100", joined)  # незнакомое окно скрыто по умолчанию

    def test_dollar_window_when_enabled(self):
        self.w.cfg["shown_extra"] = ["iguana_necktie"]
        self.w.data = DATA
        self.w.canvas.calls.clear()
        self.w.render()
        self.assertIn("$100 / $100", " | ".join(self.texts()))

    def test_click_toggles_reset_format_and_drag_does_not(self):
        e = types.SimpleNamespace(x_root=100, y_root=100)
        before = self.w.cfg["reset_abs"]
        self.w._press_start(e)
        self.w._press_end(e)
        self.assertNotEqual(before, self.w.cfg["reset_abs"])
        mid = self.w.cfg["reset_abs"]
        self.w._press_start(e)
        self.w._drag_move(types.SimpleNamespace(x_root=160, y_root=140))
        self.w._press_end(e)
        self.assertEqual(mid, self.w.cfg["reset_abs"])

    def test_menu_actions(self):
        for key in ("compact", "pulse", "snap", "ghost", "rounded", "topmost", "reset_abs", "show_breakdown"):
            self.w._vars[key].set(not self.w.cfg[key])
            self.w._toggled(key)
        for key, value in (("view", "rings"), ("size", "L"), ("theme", "light"), ("accent", "Синий"),
                           ("alpha", "75")):
            self.w._vars[key].set(value)
            self.w._radio_changed(key)
        self.assertEqual(self.w.cfg["alpha"], 0.75)
        self.w.data = DATA
        self.w._rebuild_filter_menu()
        self.assertEqual(set(self.w._filter_vars), {"five_hour", "seven_day", "iguana_necktie"})
        self.w._filter_vars["five_hour"].set(False)
        self.w._filter_vars["iguana_necktie"].set(True)
        self.w._filter_changed()
        self.assertEqual(self.w.cfg["hidden"], ["five_hour"])
        self.assertEqual(self.w.cfg["shown_extra"], ["iguana_necktie"])
        self.w.show_raw()

    def test_snap_and_enter_leave(self):
        self.w._snap()
        self.w._enter(None)
        self.assertTrue(self.w.hovering)
        self.w._leave(None)
        self.w._collapse()
        self.assertFalse(self.w.hovering)


class LoginButton(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        lw.CONFIG = Path(self.tmp.name) / "cfg.json"
        core.SNAPSHOT = Path(self.tmp.name) / "none.json"
        self.calls = []
        self._saved = core.refresh_login
        core.refresh_login = lambda: (self.calls.append(1) or (True, "Вход обновлён"))
        self.w = lw.Widget()
        self.w.cfg.update(mode="live", auto_login=False)
        self.w.data = core.Data(error="Токен истёк. Запусти claude, он обновит его.", auth_error=True)

    def tearDown(self):
        core.refresh_login = self._saved
        self.tmp.cleanup()

    def wait_login(self):
        for _ in range(100):
            try:
                self.w._login_done(*self.w.login_results.get(timeout=0.05))
                return
            except Exception:
                pass
        self.fail("login did not finish")

    def test_button_drawn_and_clickable(self):
        self.w.render()
        self.assertEqual(len(self.w._buttons), 1)
        x1, y1, x2, y2, _ = self.w._buttons[0]
        click = types.SimpleNamespace(x=(x1 + x2) // 2, y=(y1 + y2) // 2, x_root=0, y_root=0)
        self.w._press_start(click)
        self.w._press_end(click)
        self.assertTrue(self.w.login_busy)
        self.wait_login()
        self.assertEqual(self.calls, [1])
        self.assertFalse(self.w.login_busy)
        self.assertEqual(self.w.login_fails, 0)
        self.w.render()  # во время «Обновляю…» кнопка не кликабельна
        self.assertIn("Вход обновлён", " | ".join(k.get("text", "") for n, a, k in self.w.canvas.calls
                                                  if n == "create_text"))

    def test_no_button_without_auth_error_or_in_file_mode(self):
        self.w.data = core.Data(error="x")
        self.w.render()
        self.assertEqual(self.w._buttons, [])
        self.w.data = core.Data(error="Токен истёк", auth_error=True)
        self.w.cfg["mode"] = "file"
        self.w.render()
        self.assertEqual(self.w._buttons, [])

    def test_auto_login_respects_switch_cooldown_and_fail_limit(self):
        self.w.cfg["auto_login"] = True
        self.w.last_login_try = 0
        self.w._maybe_auto_login()
        self.assertTrue(self.w.login_busy)
        self.wait_login()
        self.w._maybe_auto_login()  # кулдаун: сразу снова нельзя
        self.assertFalse(self.w.login_busy)
        self.w.last_login_try = 0
        self.w.login_fails = lw.AUTO_LOGIN_MAX_FAILS
        self.w._maybe_auto_login()  # лимит неудач
        self.assertFalse(self.w.login_busy)
        self.w.cfg["auto_login"] = False
        self.w.login_fails = 0
        self.w._maybe_auto_login()  # выключено
        self.assertFalse(self.w.login_busy)

    def test_failed_login_counts(self):
        core.refresh_login = lambda: (False, "Токен не обновился")
        self.w.start_login(manual=False)
        self.wait_login()
        self.assertEqual(self.w.login_fails, 1)
        self.w.start_login(manual=True)  # ручной запуск сбрасывает счётчик
        self.assertEqual(self.w.login_fails, 0)
        self.wait_login()


class Monitors(unittest.TestCase):
    PRIMARY = (0, 0, 1920, 1040)
    RIGHT = (1920, 0, 3840, 1040)
    LEFT = (-1920, 0, 0, 1040)
    ABOVE = (0, -1080, 1920, 0)

    def test_pick_monitor(self):
        rects = [self.PRIMARY, self.RIGHT]
        self.assertEqual(lw.pick_monitor(rects, 100, 100), self.PRIMARY)
        self.assertEqual(lw.pick_monitor(rects, 2500, 500), self.RIGHT)
        self.assertEqual(lw.pick_monitor(rects, 5000, 500), self.RIGHT)  # вне экранов: ближайший
        self.assertEqual(lw.pick_monitor([self.PRIMARY, self.LEFT], -50, 10), self.LEFT)

    def test_snap_uses_the_monitor_the_window_is_on(self):
        # окно у левого края ВТОРОГО монитора прилипает к его краю, а не к краю основного
        x, y = lw.snap_position(1930, 500, 270, 150, self.RIGHT, 8, 22)
        self.assertEqual((x, y), (1928, 500))
        # у правого края второго монитора
        x, y = lw.snap_position(3840 - 270 - 5, 500, 270, 150, self.RIGHT, 8, 22)
        self.assertEqual(x, 3840 - 270 - 8)
        # у верха и низа
        self.assertEqual(lw.snap_position(2500, 10, 270, 150, self.RIGHT, 8, 22)[1], 8)
        self.assertEqual(lw.snap_position(2500, 1040 - 150 - 3, 270, 150, self.RIGHT, 8, 22)[1], 1040 - 150 - 8)
        # посреди экрана не трогаем
        self.assertEqual(lw.snap_position(2500, 500, 270, 150, self.RIGHT, 8, 22), (2500, 500))
        # монитор слева от основного: отрицательные координаты
        self.assertEqual(lw.snap_position(-1915, 300, 270, 150, self.LEFT, 8, 22), (-1912, 300))

    def test_clamp_keeps_second_monitor_position(self):
        rects = [self.PRIMARY, self.RIGHT, self.LEFT, self.ABOVE]
        for pos in [(2500, 300), (-1500, 400), (800, -700), (100, 100)]:
            self.assertEqual(lw.clamp_into(*pos, 270, 150, rects), pos)

    def test_clamp_moves_window_when_monitor_is_gone(self):
        # второй монитор отключили: окно возвращается на оставшийся, целиком в его пределах
        x, y = lw.clamp_into(2500, 300, 270, 150, [self.PRIMARY])
        self.assertTrue(0 <= x <= 1920 - 270 and 0 <= y <= 1040 - 150)
        x, y = lw.clamp_into(-1500, 300, 270, 150, [self.PRIMARY])
        self.assertEqual(x, 0)

    def test_widget_keeps_position_on_second_monitor(self):
        tmp = tempfile.TemporaryDirectory()
        lw.CONFIG = Path(tmp.name) / "cfg.json"
        core.SNAPSHOT = Path(tmp.name) / "none.json"
        saved = lw.monitor_rects
        lw.monitor_rects = lambda: [self.PRIMARY, self.RIGHT]
        try:
            w = lw.Widget()
            w.cfg.update(x=2500, y=300, snap=True)
            w._place()
            geo = [a[0] for n, a, k in w.root.calls if n == "geometry"][-1]
            self.assertEqual(geo, "+2500+300")
        finally:
            lw.monitor_rects = saved
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
