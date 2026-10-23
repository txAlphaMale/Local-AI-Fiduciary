# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_last_good.py -- (1) an optional Monarch call must not trip the 300 s breaker, (2) the newest good portfolio is saved and served,
marked stale, while the live refresh is failing (2026-10-23: one slow cashflow query blanked the dashboard for five minutes)."""
import glob, json, os, re, sys, tempfile, time, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="lastgood-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class TestBreaker(unittest.TestCase):
    def setUp(self):
        self.saved = dict(appmod._monarch_state)
        appmod._monarch_state.update(fail_ts=0.0, err="")
        self.p = [mock.patch.object(appmod, "_monarch_guard", lambda: None),
                  mock.patch.object(appmod.requests, "post", side_effect=appmod.requests.HTTPError("500"))]
        for p in self.p: p.start()
    def tearDown(self):
        for p in self.p: p.stop()
        appmod._monarch_state.update(self.saved)

    def test_a_core_call_failure_trips_the_breaker(self):
        with self.assertRaises(Exception):
            appmod._post("get_accounts", {})
        self.assertGreater(appmod._monarch_state["fail_ts"], 0)

    def test_an_optional_call_failure_does_not(self):
        with self.assertRaises(Exception):
            appmod._post("get_cashflow", {}, trip=False)
        self.assertEqual(appmod._monarch_state["fail_ts"], 0.0)

    def test_cashflow_helper_swallows_the_failure_and_leaves_the_breaker_alone(self):
        self.assertEqual(appmod._cashflow_annual(), (None, None))
        self.assertEqual(appmod._monarch_state["fail_ts"], 0.0)
        # and the real guard would still let the holdings calls through afterwards
        self.assertIn('trip=False)', open(os.path.join(_HERE, "app.py"), encoding="utf-8").read().split("def _cashflow_annual")[1][:900])


class Mem:
    def __init__(self): self.d = {}
    def __enter__(self):
        self.p = [mock.patch.object(appmod.store, "read_json_strict", lambda path: self.d[path] if path in self.d else (_ for _ in ()).throw(FileNotFoundError())),
                  mock.patch.object(appmod.store, "write_json", lambda path, v: self.d.__setitem__(path, json.loads(json.dumps(v))))]
        for p in self.p: p.start()
        return self
    def __exit__(self, *a):
        for p in self.p: p.stop()


def good(provider="monarch", t=None):
    return {"generated_at": int(t or time.time()), "net_worth": 100.0, "portfolio_provider": provider, "positions": [], "consolidated": []}


class TestLastGood(unittest.TestCase):
    def setUp(self):
        self.mem = Mem().__enter__()
        self.ps = [mock.patch.dict(os.environ, {"PORTFOLIO_PROVIDER": "monarch"}), mock.patch.object(appmod, "_cache", {"ts": 0.0, "data": None})]
        for p in self.ps: p.start()
        appmod._last_good_saved["ts"] = 0.0
    def tearDown(self):
        for p in self.ps: p.stop()
        self.mem.__exit__()

    def test_save_is_throttled_excludes_the_stale_flag_and_never_raises(self):
        appmod._save_last_good(dict(good(), stale={"x": 1}))
        rec = self.mem.d[appmod.LAST_GOOD_FILE]
        self.assertEqual((rec["provider"], "stale" in rec["data"]), ("monarch", False))
        first = rec["saved_at"]
        appmod._save_last_good(dict(good(), net_worth=999.0))                          # inside the throttle window: skipped
        self.assertEqual(self.mem.d[appmod.LAST_GOOD_FILE]["data"]["net_worth"], 100.0)
        appmod._last_good_saved["ts"] = 0.0
        with mock.patch.object(appmod.store, "write_json", side_effect=OSError("disk full")):
            appmod._save_last_good(good())                                            # must not raise

    def test_a_failed_build_with_nothing_in_memory_serves_the_saved_copy_marked_stale(self):
        appmod._save_last_good(good(t=1000))
        with mock.patch.object(appmod, "build", side_effect=appmod.MonarchDisabled("Monarch failed recently (HTTPError); not retrying for 224s")):
            r = appmod.portfolio()
        self.assertEqual(r["net_worth"], 100.0)
        self.assertEqual((r["stale"]["from_disk"], r["stale"]["retry_in_s"]), (True, 60))
        self.assertIn("not retrying for 224s", r["stale"]["reason"]); self.assertGreater(r["stale"]["since"], 0)

    def test_no_saved_copy_or_a_copy_from_another_provider_still_returns_the_error(self):
        with mock.patch.object(appmod, "build", side_effect=RuntimeError("boom")):
            self.assertEqual(appmod.portfolio().status_code, 502)
        appmod._save_last_good(good(provider="snaptrade"))                             # saved under a different source: never shown for Monarch
        with mock.patch.object(appmod, "build", side_effect=RuntimeError("boom")):
            self.assertEqual(appmod.portfolio().status_code, 502)

    def test_a_failed_refresh_with_data_in_memory_marks_it_stale_and_keeps_the_original_since(self):
        appmod._cache.update(data=good(t=5000), ts=0.0)
        with mock.patch.object(appmod, "build", side_effect=RuntimeError("one")):
            r1 = appmod.portfolio(refresh=1)
        self.assertEqual((r1["stale"]["since"], r1["stale"]["from_disk"]), (5000, False))
        with mock.patch.object(appmod, "build", side_effect=RuntimeError("two")):
            r2 = appmod.portfolio(refresh=1)
        self.assertEqual((r2["stale"]["since"], r2["stale"]["reason"]), (5000, "two"))

    def test_retries_about_once_a_minute_then_the_stale_flag_clears_on_success(self):
        appmod._save_last_good(good(t=1000))
        calls = []
        def failing(): calls.append(1); raise RuntimeError("down")
        with mock.patch.object(appmod, "build", failing):
            appmod.portfolio(); n1 = len(calls)
            appmod.portfolio(); appmod.portfolio()                                     # within the minute: served from memory, no rebuild
            self.assertEqual(len(calls), n1)
            appmod._cache["ts"] -= 61
            appmod.portfolio(); self.assertEqual(len(calls), n1 + 1)                  # a minute later: tries again
        with mock.patch.object(appmod, "build", lambda: good(t=9000)):
            appmod._cache["ts"] -= 61
            r = appmod.portfolio()
        self.assertNotIn("stale", r); self.assertEqual(r["generated_at"], 9000)

    def test_reason_is_single_line_and_bounded(self):
        s = appmod._mark_stale(good(), "line1\nline2 " + "x" * 500, 1, False)["stale"]["reason"]
        self.assertNotIn("\n", s); self.assertLessEqual(len(s), 220)

    def test_build_saves_after_a_good_run(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn("_snapshot_shares(d)         # record today's per-ticker shares for the wash-sale ledger (GAP-4)\n    _save_last_good(d)", src)


class TestUi(unittest.TestCase):
    def test_banner_and_header(self):
        for needle in ("function staleBannerNode()", "Showing your last good portfolio from", "body.appendChild(staleBannerNode());", "(d.stale?' · STALE':'')"):
            self.assertIn(needle, HTML)


if __name__ == "__main__":
    unittest.main()
