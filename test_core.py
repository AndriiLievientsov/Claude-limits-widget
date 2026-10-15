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


class Pace(unittest.TestCase):
    H = 3600

    def win(self, pct, left, kind="five_hour"):
        return core.Window(kind, pct, NOW + left)

    def test_pace_is_share_of_elapsed_time(self):
        self.assertAlmostEqual(core.pace_percent(self.win(10, 4 * self.H), NOW), 20)
        self.assertAlmostEqual(core.pace_percent(self.win(10, 3.5 * 86400, "seven_day"), NOW), 50)
        self.assertIsNone(core.pace_percent(core.Window("five_hour", 10, None), NOW))
        self.assertIsNone(core.pace_percent(self.win(10, self.H, "spend_limit"), NOW))
        self.assertEqual(core.pace_percent(self.win(10, 9 * self.H), NOW), 0)  # странный сброс: не уходим в минус

    def test_forecast_when_too_fast(self):
        # прошёл 1 ч из 5, израсходовано 50% → оставшиеся 50% уйдут ещё за 1 ч
        self.assertAlmostEqual(core.forecast_exhaust(self.win(50, 4 * self.H), NOW), NOW + self.H)

    def test_no_forecast_when_enough_or_unknown(self):
        self.assertIsNone(core.forecast_exhaust(self.win(10, 4 * self.H), NOW))  # хватит до сброса
        self.assertIsNone(core.forecast_exhaust(self.win(0, 4 * self.H), NOW))
        self.assertIsNone(core.forecast_exhaust(self.win(5, 5 * self.H - 60), NOW))  # слишком рано
        self.assertIsNone(core.forecast_exhaust(core.Window("five_hour", 50, None), NOW))

    def test_exhausted_now(self):
        self.assertEqual(core.forecast_exhaust(self.win(100, self.H), NOW), NOW)


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "h.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def data(self, pct, t):
        return core.Data([core.Window("five_hour", pct, NOW + 3600)], updated_at=t)

    def test_record_shape_and_skips(self):
        rec = core.history_record(self.data(12.345, NOW))
        self.assertEqual(rec, {"t": NOW, "w": {"five_hour": [12.35, NOW + 3600]}})
        self.assertIsNone(core.history_record(core.Data(error="x")))
        self.assertIsNone(core.history_record(core.Data([core.Window("five_hour", 1, None)], error="429")))

    def test_should_record(self):
        a = core.history_record(self.data(10, NOW))
        self.assertTrue(core.should_record(a, None))
        self.assertFalse(core.should_record(a, a))  # тот же снимок
        self.assertFalse(core.should_record(core.history_record(self.data(10, NOW + 60)), a))  # без изменений
        self.assertTrue(core.should_record(core.history_record(self.data(11, NOW + 60)), a))
        self.assertTrue(core.should_record(core.history_record(self.data(10, NOW + core.HISTORY_HEARTBEAT)), a))
        self.assertFalse(core.should_record(None, a))

    def test_append_read_and_rotate(self):
        saved = core.HISTORY_MAX_BYTES
        core.HISTORY_MAX_BYTES = 200
        try:
            for i in range(10):
                self.assertIsNone(core.append_history(core.history_record(self.data(i, NOW + i)), self.path))
        finally:
            core.HISTORY_MAX_BYTES = saved
        self.assertTrue(self.path.with_name("h.jsonl.1").exists())
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("not json\n")
        recs = core.read_history(self.path)
        self.assertEqual([r["t"] for r in recs], sorted(r["t"] for r in recs))
        self.assertEqual(recs[-1]["w"]["five_hour"][0], 9)
        self.assertTrue(all(r["t"] >= NOW + 5 for r in core.read_history(self.path, since=NOW + 5)))

    def test_append_error_is_returned(self):
        self.assertIn("История", core.append_history({"t": 1, "w": {}}, Path(self.tmp.name) / "no" / "h.jsonl"))


class Alerts(unittest.TestCase):
    def step(self, state, pct, kind="five_hour"):
        return core.check_alerts([core.Window(kind, pct, NOW + 3600)], state)

    def test_first_sight_is_silent(self):
        msgs, st = self.step({}, 95)
        self.assertEqual(msgs, [])
        self.assertEqual(st["five_hour"]["level"], 90)

    def test_thresholds_fire_once(self):
        _, st = self.step({}, 50)
        msgs, st = self.step(st, 81)
        self.assertEqual(msgs, ["5 часов: израсходовано 81%"])
        msgs, st = self.step(st, 85)
        self.assertEqual(msgs, [])
        msgs, st = self.step(st, 97)  # сразу через оба порога — одно уведомление
        self.assertEqual(len(msgs), 1)
        msgs, st = self.step(st, 99)
        self.assertEqual(msgs, [])

    def test_reset_after_threshold(self):
        _, st = self.step({}, 50)
        _, st = self.step(st, 92)
        msgs, st = self.step(st, 0)
        self.assertEqual(msgs, ["5 часов: лимит сброшен, сейчас 0%"])
        msgs, st = self.step(st, 82)  # в новом окне пороги снова работают
        self.assertEqual(len(msgs), 1)

    def test_reset_of_low_window_is_silent(self):
        _, st = self.step({}, 40)
        msgs, _ = self.step(st, 0)
        self.assertEqual(msgs, [])


class Sessions(unittest.TestCase):
    RAW = {"a": {"model": "Opus", "cost_usd": 1.5, "duration_ms": 3_900_000, "context_percent": 42,
                 "cache_read": 900, "cache_create": 60, "input_tokens": 40, "folder": "proj",
                 "updated_at": NOW - 10},
           "b": {"model": "Haiku", "updated_at": NOW - 100},
           "old": {"model": "X", "updated_at": NOW - core.SESSION_TTL - 1},
           "bad": "x", "nots": {"model": "Y"}}

    def test_parse_sorted_and_filtered(self):
        out = core.parse_sessions(self.RAW, NOW)
        self.assertEqual([x.id for x in out], ["a", "b"])
        a = out[0]
        self.assertEqual((a.model, a.cost_usd, a.folder), ("Opus", 1.5, "proj"))
        self.assertAlmostEqual(a.cache_percent, 90.0)
        self.assertIsNone(out[1].cache_percent)

    def test_parse_garbage(self):
        self.assertEqual(core.parse_sessions(None, NOW), [])
        self.assertEqual(core.parse_sessions([1], NOW), [])

    def test_cache_percent(self):
        self.assertIsNone(core.cache_percent(None, 5, 5))
        self.assertIsNone(core.cache_percent(0, 0, 0))
        self.assertEqual(core.cache_percent(50, 25, 25), 50)

    def test_duration(self):
        self.assertEqual(core.format_duration(30_000), "<1 мин")
        self.assertEqual(core.format_duration(42 * 60000), "42 мин")
        self.assertEqual(core.format_duration(65 * 60000), "1 ч 05 мин")
        self.assertEqual(core.format_duration(None), "")

    def test_compact_hint(self):
        self.assertIsNone(core.compact_hint(None))
        self.assertIsNone(core.compact_hint(50))
        self.assertIn("compact", core.compact_hint(75))
        self.assertIn("clear", core.compact_hint(95))

    def test_snapshot_carries_sessions(self):
        with tempfile.TemporaryDirectory() as t:
            core.SNAPSHOT = Path(t) / "s.json"
            core.SNAPSHOT.write_text(json.dumps({"saved_at": time.time(), "sessions": {"s": {
                "model": "Opus", "updated_at": time.time()}},
                "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": time.time() + 600}}}))
            self.assertEqual([x.id for x in core.read_snapshot().sessions], ["s"])


class Statusline(unittest.TestCase):
    def test_build_session(self):
        import limits_statusline as sl
        s = sl.build_session({"session_id": "x", "model": {"display_name": "Opus 4"},
                              "cost": {"total_cost_usd": 0.5, "total_duration_ms": 1000},
                              "context_window": {"used_percentage": 12, "current_usage": {
                                  "cache_read_input_tokens": 8, "input_tokens": 2}},
                              "workspace": {"current_dir": "C:/a/proj"}}, NOW)
        self.assertEqual((s["id"], s["model"], s["cost_usd"], s["cache_read"]), ("x", "Opus 4", 0.5, 8.0))
        self.assertEqual(s["folder"], "proj")
        self.assertIsNone(sl.build_session({}, NOW))
        self.assertIsNone(sl.build_session({"session_id": "x", "cost": "bad"}, NOW)["cost_usd"])

    def test_merge_limits_and_drops_old(self):
        import limits_statusline as sl
        old = {f"s{i}": {"id": f"s{i}", "updated_at": NOW - i} for i in range(7)}
        old["stale"] = {"id": "stale", "updated_at": NOW - sl.SESSION_TTL - 5}
        merged = sl.merge_sessions(old, {"id": "new", "updated_at": NOW}, NOW)
        self.assertEqual(len(merged), sl.MAX_SESSIONS)
        self.assertIn("new", merged); self.assertNotIn("stale", merged)
        self.assertEqual(sl.merge_sessions(None, None, NOW), {})


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
