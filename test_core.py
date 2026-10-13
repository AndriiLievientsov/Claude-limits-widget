import json, subprocess, time, types, unittest, tempfile
from pathlib import Path
import limits_core as core

NOW = 1_800_000_000.0


class Parse(unittest.TestCase):
    def test_claude_code_shape(self):
        w = core.parse_windows({"five_hour": {"used_percentage": 23.5, "resets_at": NOW + 3600},
                                "seven_day": {"used_percentage": 41, "resets_at": NOW + 86400 * 3}}, NOW)
        self.assertEqual([x.kind for x in w], ["five_hour", "seven_day"])
        self.assertEqual(w[0].percent, 23.5)

    def test_api_shape_iso(self):
        w = core.parse_windows({"five_hour": {"utilization": 10, "resets_at": "2027-01-15T10:00:00Z"}}, 0)
        self.assertEqual(w[0].percent, 10)
        self.assertAlmostEqual(w[0].resets_at, 1800007200.0, delta=1)

    def test_passed_reset_zeroes(self):
        w = core.parse_windows({"five_hour": {"used_percentage": 90, "resets_at": NOW - 5}}, NOW)
        self.assertEqual((w[0].percent, w[0].resets_at), (0.0, None))

    def test_garbage(self):
        self.assertEqual(core.parse_windows({"five_hour": "x", "seven_day": {}}, NOW), [])
        self.assertEqual(core.parse_windows(None, NOW), [])

    def test_format_reset(self):
        self.assertEqual(core.format_reset(NOW + 90, NOW), "1 мин")
        self.assertEqual(core.format_reset(NOW + 2 * 3600 + 600, NOW), "2 ч 10 мин")
        self.assertEqual(core.format_reset(NOW + 86400 * 3, NOW), "3 дн")
        self.assertEqual(core.format_reset(NOW - 1, NOW), "сброс")

    def test_unknown_windows_are_discovered_and_ordered(self):
        rl = {"zeta": {"utilization": 3}, "seven_day_opus": {"utilization": 20},
              "five_hour": {"utilization": 50}, "seven_day": {"utilization": 30},
              "extra": "text", "none_pct": {"utilization": None}}
        w = core.parse_windows(rl, NOW)
        self.assertEqual([x.kind for x in w], ["five_hour", "seven_day", "seven_day_opus", "zeta"])

    def test_labels(self):
        self.assertEqual(core.label_for("seven_day_opus"), "Неделя · Opus")
        self.assertEqual(core.label_for("seven_day_omelette"), "Seven day omelette")

    def test_colors(self):
        self.assertEqual([core.color_for(p) for p in (10, 70, 95)], ["#30a46c", "#f5a524", "#e5484d"])


class Helpers(unittest.TestCase):
    def test_mix(self):
        self.assertEqual(core.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(core.mix("#102030", "#102030", 0.9), "#102030")
        self.assertEqual(core.mix("#ff0000", "#0000ff", 0), "#ff0000")

    def test_abs_reset(self):
        import time as t
        now = t.time()
        self.assertTrue(core.format_reset_abs(now + 3600, now).startswith("в "))
        far = core.format_reset_abs(now + 3 * 86400, now)
        self.assertIn(far.split()[0], core.WEEKDAYS)
        self.assertEqual(core.format_reset_abs(now - 1, now), "сброс")
        self.assertEqual(core.format_reset_abs(None, now), "")

    def test_normal_color_and_short(self):
        self.assertEqual(core.color_for(10, None, "#3b82f6"), "#3b82f6")
        self.assertEqual(core.color_for(95, None, "#3b82f6"), "#e5484d")
        self.assertEqual(core.short_label("five_hour"), "5 ч")
        self.assertEqual(core.short_label("foo_bar"), "Foo bar")


REAL = {
    "five_hour": {"utilization": 57.0, "resets_at": "2099-10-12T11:40:00+00:00", "limit_dollars": None},
    "seven_day": {"utilization": 39.0, "resets_at": "2099-10-18T15:00:00+00:00"},
    "seven_day_opus": None, "tangelo": None,
    "iguana_necktie": {"utilization": 100.0, "resets_at": "2099-11-05T07:59:00+00:00",
                       "limit_dollars": 100, "used_dollars": 100.284535, "remaining_dollars": 0.0},
    "limits": [{"kind": "session", "severity": "normal", "percent": 57},
               {"kind": "weekly_all", "severity": "critical", "percent": 39}],
    "seven_day_breakdown": {"rows": [{"key": "claude_code", "display_name": "Claude Code", "percent": 17},
                                     {"key": "chat", "display_name": "Chats", "percent": 0},
                                     {"key": "cowork", "display_name": "Cowork", "percent": 83}]},
}


class RealShape(unittest.TestCase):
    def test_windows(self):
        w = core.parse_windows(REAL, 0)
        self.assertEqual([x.kind for x in w], ["five_hour", "seven_day", "iguana_necktie"])
        self.assertEqual((w[2].used_dollars, w[2].limit_dollars), (100.284535, 100.0))
        self.assertIsNone(w[0].used_dollars)
        self.assertEqual((w[0].severity, w[1].severity), ("normal", "critical"))

    def test_breakdown(self):
        b = core.parse_breakdown(REAL)
        self.assertEqual([(x.name, x.percent) for x in b], [("Code", 17.0), ("Чаты", 0.0), ("Cowork", 83.0)])
        self.assertEqual(core.parse_breakdown({}), [])
        self.assertEqual(core.parse_breakdown(None), [])

    def test_severity_color(self):
        self.assertEqual(core.color_for(10, "critical"), "#e5484d")
        self.assertEqual(core.color_for(10, "warning"), "#f5a524")
        self.assertEqual(core.color_for(10, "normal"), "#30a46c")


class RefreshLogin(unittest.TestCase):
    def setUp(self):
        self._saved = (core.token_ok, core.find_claude, core.run_claude)

    def tearDown(self):
        core.token_ok, core.find_claude, core.run_claude = self._saved

    def fake(self, ok_after, exe="claude"):
        calls = []
        state = {"n": 0}

        def token_ok():
            return ok_after is not None and state["n"] >= ok_after

        def run(exe_, args, timeout):
            calls.append(args)
            state["n"] += 1
            return types.SimpleNamespace(stdout="", stderr="boom: not logged in\nmore")

        core.token_ok, core.find_claude, core.run_claude = token_ok, (lambda: exe), run
        return calls

    def test_already_valid(self):
        core.token_ok = lambda: True
        self.assertEqual(core.refresh_login(), (True, "Вход уже действителен"))

    def test_first_step_enough(self):
        calls = self.fake(ok_after=1)
        self.assertEqual(core.refresh_login(), (True, "Вход обновлён"))
        self.assertEqual(calls, [["auth", "status"]])

    def test_second_step_needed(self):
        calls = self.fake(ok_after=2)
        self.assertEqual(core.refresh_login()[0], True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], "-p")

    def test_never_ok(self):
        self.fake(ok_after=None)
        ok, msg = core.refresh_login()
        self.assertFalse(ok)
        self.assertIn("не обновился", msg)
        self.assertIn("boom", msg)

    def test_no_claude(self):
        self.fake(ok_after=None, exe=None)
        ok, msg = core.refresh_login()
        self.assertFalse(ok)
        self.assertIn("Не найден claude", msg)

    def test_timeout(self):
        self.fake(ok_after=None)

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("claude", 1)

        core.run_claude = boom
        self.assertIn("не ответил", core.refresh_login(timeout=1)[1])

    def test_auth_error_flag(self):
        with tempfile.TemporaryDirectory() as t:
            core.CREDENTIALS = Path(t) / "none.json"
            d = core.fetch_live()
            self.assertTrue(d.auth_error)


class Files(unittest.TestCase):
    def test_snapshot_roundtrip_and_errors(self):
        with tempfile.TemporaryDirectory() as t:
            core.SNAPSHOT = Path(t) / "s.json"
            self.assertIn("Нет данных", core.read_snapshot().error)
            core.SNAPSHOT.write_text(json.dumps({"saved_at": time.time(), "context_percent": 12,
                "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": time.time() + 600}}}))
            d = core.read_snapshot()
            self.assertIsNone(d.error); self.assertEqual(d.context_percent, 12)
            core.SNAPSHOT.write_text("{bad")
            self.assertIn("Не прочитать", core.read_snapshot().error)

    def test_token(self):
        with tempfile.TemporaryDirectory() as t:
            core.CREDENTIALS = Path(t) / "c.json"
            self.assertIn("Нет файла", core.read_token()[1])
            core.CREDENTIALS.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x", "expiresAt": 1}}))
            self.assertIn("истёк", core.read_token()[1])
            core.CREDENTIALS.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x",
                "expiresAt": (time.time() + 999) * 1000}}))
            self.assertEqual(core.read_token(), ("x", None))


if __name__ == "__main__":
    unittest.main()
