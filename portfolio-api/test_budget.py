# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_budget.py -- API budget gate (budget.py): pause on quota / rate limit, probe, recover, soft budgets,
persistence, plus the Tavily /usage probe and the app wiring (deferred != no data)."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import budget as B  # noqa: E402


class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s


def mk(**kw):
    c = Clock()
    return B.Budget(clock=c, **kw), c


class TestPauseAndRecover(unittest.TestCase):
    def test_open_allows_and_success_keeps_open(self):
        b, c = mk()
        self.assertTrue(b.check("tavily")["ok"])
        b.report("tavily", status=200)
        self.assertEqual(b.status()[0]["state"], "open")

    def test_quota_code_pauses_then_denies_as_deferred(self):
        b, c = mk()
        b.report("tavily", status=432)
        r = b.check("tavily")
        self.assertFalse(r["ok"]); self.assertTrue(r["deferred"])
        self.assertIn("paused", r["reason"]); self.assertIn("quota", r["reason"])
        st = b.status()[0]
        self.assertEqual((st["state"], st["kind"]), ("paused", "quota"))

    def test_rate_limit_honours_retry_after_and_caps(self):
        b, c = mk()
        b.report("finnhub", status=429, retry_after=300)
        self.assertAlmostEqual(b.status()[1]["paused_until"] - c.t, 300, delta=1)
        b2, c2 = mk()
        b2.report("finnhub", status=429)
        self.assertAlmostEqual(b2.status()[1]["paused_until"] - c2.t, 60, delta=1)

    def test_backoff_doubles_and_is_capped(self):
        b, c = mk()
        secs = []
        for _ in range(8):
            b.report("finnhub", status=429)
            secs.append(round(b.status()[1]["paused_until"] - c.t))
            c.adv(secs[-1] + 1)
            b.check("finnhub")                 # expired -> half-open trial
        self.assertEqual(secs[:3], [60, 120, 240])
        self.assertEqual(max(secs), 3600)

    def test_no_probe_means_exactly_one_trial_call_then_success_reopens(self):
        b, c = mk()
        b.report("finnhub", status=429)
        c.adv(61)
        first = b.check("finnhub")
        self.assertTrue(first["ok"]); self.assertTrue(first.get("trial"))
        self.assertFalse(b.check("finnhub")["ok"])      # second caller waits while the trial is in flight
        b.report("finnhub", status=200)
        self.assertEqual(b.status()[1]["state"], "open")
        self.assertTrue(b.check("finnhub")["ok"])

    def test_failed_trial_extends_the_pause(self):
        b, c = mk()
        b.report("finnhub", status=429)
        c.adv(61); b.check("finnhub")
        b.report("finnhub", status=429)
        self.assertGreater(b.status()[1]["paused_until"] - c.t, 100)

    def test_probe_recovers_without_spending_a_call(self):
        b, c = mk()
        b.register_probe("tavily", lambda: {"state": "ok", "usage": {"plan_usage": 10, "plan_limit": 1000}})
        b.report("tavily", status=432)
        c.adv(3601)
        self.assertTrue(b.check("tavily")["ok"])
        st = b.status()[0]
        self.assertEqual(st["state"], "open"); self.assertEqual(st["usage"]["plan_limit"], 1000)

    def test_probe_still_exhausted_keeps_paused_and_lengthens(self):
        b, c = mk()
        b.register_probe("tavily", lambda: {"state": "quota", "detail": "plan credits used up (1000/1000)"})
        b.report("tavily", status=432)
        c.adv(3601)
        r = b.check("tavily")
        self.assertFalse(r["ok"]); self.assertIn("used up", r["reason"])
        self.assertGreater(b.status()[0]["paused_until"] - c.t, 7000)

    def test_probe_exception_is_contained(self):
        b, c = mk()
        def boom(): raise RuntimeError("x")
        b.register_probe("tavily", boom)
        b.report("tavily", status=432)
        c.adv(3601)
        self.assertFalse(b.check("tavily")["ok"])

    def test_known_renewal_caps_the_wait(self):
        b, c = mk()
        b.configure("tavily", renewal=c.t + 1800)
        b.report("tavily", status=432)
        self.assertAlmostEqual(b.status()[0]["paused_until"] - c.t, 1800, delta=1)

    def test_other_4xx_never_pause(self):
        b, c = mk()
        for code in (400, 401, 403):
            b.report("finnhub", status=code)
        self.assertEqual(b.status()[1]["state"], "open")
        b.report("sec", status=403)               # SEC: 403 IS its throttle signal
        self.assertEqual(b.status()[2]["state"], "paused")

    def test_repeated_network_failures_pause_as_outage(self):
        b, c = mk()
        for _ in range(4):
            b.report("tavily", status=None)
        self.assertEqual(b.status()[0]["state"], "open")
        b.report("tavily", status=503)
        st = b.status()[0]
        self.assertEqual((st["state"], st["kind"]), ("paused", "outage"))
        b2, _ = mk()
        for _ in range(4): b2.report("tavily", status=None)
        b2.report("tavily", status=200)           # a success resets the streak
        for _ in range(4): b2.report("tavily", status=None)
        self.assertEqual(b2.status()[0]["state"], "open")


class TestPacingAndSoftBudget(unittest.TestCase):
    def test_interactive_pace_and_window_slides(self):
        b, c = mk()
        lim = B.PROVIDERS["finnhub"]["hard_per_min"]
        for _ in range(lim):
            self.assertTrue(b.check("finnhub")["ok"])
        r = b.check("finnhub")
        self.assertFalse(r["ok"]); self.assertIn("pacing", r["reason"])
        c.adv(61)
        self.assertTrue(b.check("finnhub")["ok"])

    def test_batch_is_paced_lower_than_interactive(self):
        b, c = mk()
        lim = B.PROVIDERS["finnhub"]["batch_per_min"]
        for _ in range(lim):
            self.assertTrue(b.check("finnhub", batch=True)["ok"])
        self.assertFalse(b.check("finnhub", batch=True)["ok"])
        self.assertTrue(b.check("finnhub")["ok"])       # interactive still has headroom

    def test_batch_daily_soft_budget_resets_next_day(self):
        b, c = mk()
        b.configure("tavily", batch_daily=2)
        c.t = 86400 * 100 + 10
        self.assertTrue(b.check("tavily", batch=True)["ok"])
        self.assertTrue(b.check("tavily", batch=True)["ok"])
        r = b.check("tavily", batch=True)
        self.assertFalse(r["ok"]); self.assertIn("daily soft budget", r["reason"])
        self.assertTrue(b.check("tavily")["ok"])        # interactive jobs never consume the batch budget
        c.t = 86400 * 101 + 10
        self.assertTrue(b.check("tavily", batch=True)["ok"])

    def test_pause_beats_everything(self):
        b, c = mk()
        b.report("tavily", status=429)
        self.assertFalse(b.check("tavily")["ok"])
        self.assertFalse(b.check("tavily", batch=True)["ok"])


def ok(b, n=1, **kw):
    for _ in range(n):
        b.report("tavily", status=200, **kw)


class TestSpend(unittest.TestCase):
    """Tavily pay-as-you-go (2026-11-12): 1,000 free credits, app-side dollar cap beyond them, scavenger held to the
    free allowance, every credit logged by source."""
    def test_credits_are_logged_by_source(self):
        b, c = mk()
        ok(b, 3, source="chat"); ok(b, 2, source="brief")
        sp = b.status()[0]["spend"]
        self.assertEqual(sp["credits_used"], 5)
        self.assertEqual(sp["by_source"], {"chat": 3, "brief": 2})

    def test_failed_calls_cost_nothing(self):
        b, c = mk()
        b.report("tavily", status=500, source="chat")
        self.assertEqual(b.status()[0]["spend"]["credits_used"], 0)

    def test_hard_dollar_cap_stops_every_caller(self):
        b, c = mk()
        b.configure("tavily", hard_cap_usd=0.80)             # 100 credits beyond the 1,000 free
        ok(b, 1099, source="chat")
        self.assertTrue(b.check("tavily")["ok"])
        ok(b, 1, source="chat")                               # 1,100 = cap
        r = b.check("tavily")
        self.assertFalse(r["ok"]); self.assertIn("spend cap", r["reason"]); self.assertIn("$0.8", r["reason"])
        self.assertFalse(b.check("tavily", batch=True)["ok"])
        self.assertEqual(b.status()[0]["spend"]["credits_left_before_cap"], 0)
        b.configure("tavily", hard_cap_usd=5)                 # raising the cap re-opens it
        self.assertTrue(b.check("tavily")["ok"])

    def test_default_cap_is_eight_dollars_2000_credits(self):
        b, c = mk()
        sp = b.status()[0]["spend"]
        self.assertEqual(sp["hard_cap_usd"], 8.0)
        self.assertEqual(sp["credits_left_before_cap"], 2000)

    def test_scavenger_is_held_to_the_free_allowance_but_interactive_continues(self):
        b, c = mk()
        c.t = 86400 * 200 + 5
        b.configure("tavily", batch_daily=None)
        ok(b, 1000, source="chat")
        r = b.check("tavily", batch=True)
        self.assertFalse(r["ok"]); self.assertIn("scavenger is held to 1000", r["reason"])
        self.assertTrue(b.check("tavily")["ok"])              # chat/briefs may spend into pay-as-you-go
        b.configure("tavily", batch_cycle_credits=1500)
        self.assertTrue(b.check("tavily", batch=True)["ok"])

    def test_cycle_rolls_on_the_renewal_day_and_keeps_last_cycle(self):
        import datetime as dt
        b, c = mk()
        ren = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc).timestamp()
        b.configure("tavily", renewal=ren)
        c.t = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc).timestamp()
        ok(b, 40, source="chat")
        self.assertEqual(b.status()[0]["spend"]["credits_used"], 40)
        c.t = dt.datetime(2026, 12, 2, tzinfo=dt.timezone.utc).timestamp()
        sp = b.status()[0]["spend"]
        self.assertEqual(sp["credits_used"], 0)
        self.assertEqual(sp["last_cycle"]["credits"], 40)
        self.assertEqual(dt.datetime.fromtimestamp(sp["cycle_start"], dt.timezone.utc).day, 1)

    def test_usage_snapshot_never_lets_the_estimate_sit_below_the_provider(self):
        b, c = mk()
        b.register_probe("tavily", lambda: {"state": "ok", "usage": {"plan_usage": 606, "plan_limit": 1000}})
        self.assertTrue(b.check("tavily")["ok"])              # first check does the free hourly sync
        self.assertEqual(b.status()[0]["spend"]["credits_used"], 606)
        ok(b, 4)
        self.assertEqual(b.status()[0]["spend"]["credits_used"], 610)
        calls = []
        b.register_probe("tavily", lambda: calls.append(1) or {"state": "ok", "usage": {"plan_usage": 700}})
        b.check("tavily"); self.assertEqual(calls, [])        # not again within the hour
        c.adv(3601); b.check("tavily")
        self.assertEqual(len(calls), 1)
        self.assertEqual(b.status()[0]["spend"]["credits_used"], 700)

    def test_sync_probe_that_finds_the_quota_gone_pauses_early(self):
        b, c = mk()
        b.register_probe("tavily", lambda: {"state": "quota", "detail": "used up", "usage": {"plan_usage": 1000}})
        r = b.check("tavily")
        self.assertFalse(r["ok"]); self.assertEqual(b.status()[0]["kind"], "quota")

    def test_spend_settings_persist(self):
        box = {}
        c = Clock()
        mkb = lambda: B.Budget(clock=c, load=lambda: box.get("d"), save=lambda d: box.__setitem__("d", d))
        b = mkb()
        b.configure("tavily", hard_cap_usd=3, batch_cycle_credits=1200)
        ok(b, 7, source="alerts")
        sp = mkb().status()[0]["spend"]
        self.assertEqual((sp["hard_cap_usd"], sp["batch_cycle_credits"], sp["credits_used"], sp["by_source"]),
                         (3, 1200, 7, {"alerts": 7}))

    def test_non_spend_providers_have_no_spend_block(self):
        b, c = mk()
        self.assertIsNone(b.status()[1]["spend"])


class TestPersistence(unittest.TestCase):
    def test_paused_state_and_settings_survive_a_restart(self):
        box = {}
        c = Clock()
        b = B.Budget(clock=c, load=lambda: box.get("d"), save=lambda d: box.__setitem__("d", d))
        b.configure("tavily", batch_daily=7, renewal=c.t + 99999)
        b.report("tavily", status=432)
        b2 = B.Budget(clock=c, load=lambda: box.get("d"), save=lambda d: box.__setitem__("d", d))
        st = b2.status()[0]
        self.assertEqual((st["state"], st["kind"], st["batch_daily_budget"]), ("paused", "quota", 7))
        self.assertFalse(b2.check("tavily")["ok"])

    def test_broken_store_never_breaks_the_gate(self):
        def bad(*a): raise OSError("disk")
        b = B.Budget(clock=Clock(), load=bad, save=bad)
        b.report("tavily", status=432)
        self.assertFalse(b.check("tavily")["ok"])

    def test_force_probe_is_rate_limited(self):
        b, c = mk()
        b.register_probe("tavily", lambda: {"state": "ok"})
        self.assertEqual(b.force_probe("tavily")["state"], "ok")
        self.assertIn("wait", b.force_probe("tavily")["detail"])
        c.adv(61)
        self.assertEqual(b.force_probe("tavily")["state"], "ok")


class TestTavilyUsage(unittest.TestCase):
    def setUp(self):
        import tavily_client as T
        self.T = T
        T.configure(lambda: "k")

    def _resp(self, code, js=None):
        r = mock.Mock(); r.status_code = code; r.text = ""
        r.json.return_value = js
        return r

    def test_nested_shape(self):
        js = {"key": {"usage": 5, "limit": 100}, "account": {"plan_usage": 606, "plan_limit": 1000, "paygo_limit": None}}
        with mock.patch.object(self.T.requests, "get", return_value=self._resp(200, js)):
            u = self.T.usage()
        self.assertTrue(u["ok"])
        self.assertEqual((u["plan_usage"], u["plan_limit"], u["key_usage"]), (606, 1000, 5))

    def test_unparseable_numbers_come_back_none(self):
        with mock.patch.object(self.T.requests, "get", return_value=self._resp(200, {"weird": 1})):
            u = self.T.usage()
        self.assertTrue(u["ok"]); self.assertIsNone(u["plan_usage"])

    def test_paygo_fields(self):
        js = {"account": {"plan_usage": 1200, "plan_limit": 1000, "paygo_usage": 1.6, "paygo_limit": 10}}
        with mock.patch.object(self.T.requests, "get", return_value=self._resp(200, js)):
            u = self.T.usage()
        self.assertEqual((u["paygo"], u["paygo_usage"], u["paygo_limit"]), (True, 1.6, 10))

    def test_http_error_and_no_key_never_raise_or_leak(self):
        with mock.patch.object(self.T.requests, "get", return_value=self._resp(429)):
            u = self.T.usage()
        self.assertFalse(u["ok"]); self.assertEqual(u["http"], 429)
        self.T.configure(lambda: "")
        self.assertFalse(self.T.usage()["ok"])
        self.T.configure(lambda: "k")
        with mock.patch.object(self.T.requests, "get", side_effect=self.T.requests.ConnectionError("k=secret")):
            u = self.T.usage()
        self.assertNotIn("secret", u["detail"])


class TestAppWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="budget-test-")
        for f in glob.glob(os.path.join(_HERE, "*.py")):
            if os.path.basename(f).startswith("test_"):
                continue
            for m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(f).read()):
                os.environ.setdefault(m.group(1), os.path.join(tmp, m.group(2)[len("/data"):].lstrip("/")))
        os.environ.setdefault("INTERNAL_SCHEDULER", "0")
        import app as appmod
        cls.app = appmod

    def setUp(self):
        self.c = Clock()
        self.fresh = B.Budget(clock=self.c)
        self.fresh.register_probe("tavily", lambda: {"state": "ok"})
        self.p = mock.patch.object(self.app, "_budget", self.fresh)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_paused_tavily_is_deferred_not_sent(self):
        self.fresh.report("tavily", status=432)
        with mock.patch.object(self.app.tavily_client, "configured", return_value=True), \
             mock.patch.object(self.app.tavily_client, "search") as s:
            res = self.app._tavily_call("q", 7, 5)
        s.assert_not_called()
        self.assertFalse(res["ok"]); self.assertTrue(res["deferred"]); self.assertEqual(res["articles"], [])
        self.assertIn("paused", res["detail"])

    def test_tavily_quota_response_pauses_the_gate(self):
        fake = {"ok": False, "articles": [], "http": 432, "detail": "x"}
        with mock.patch.object(self.app.tavily_client, "configured", return_value=True), \
             mock.patch.object(self.app.tavily_client, "search", return_value=fake):
            self.app._tavily_call("q", 7, 5)
        self.assertEqual(self.fresh.status()[0]["state"], "paused")

    def test_no_key_never_touches_the_gate(self):
        fake = {"ok": False, "articles": [], "http": None, "detail": "No Tavily API key"}
        with mock.patch.object(self.app.tavily_client, "configured", return_value=False), \
             mock.patch.object(self.app.tavily_client, "search", return_value=fake):
            for _ in range(8):
                self.app._tavily_call("q", 7, 5)
        self.assertEqual(self.fresh.status()[0]["state"], "open")

    def test_finnhub_429_pauses_and_a_paused_call_skips_the_network(self):
        r = mock.Mock(status_code=429, headers={"Retry-After": "120"})
        with mock.patch.object(self.app, "FINNHUB", "tok"), \
             mock.patch.object(self.app.requests, "get", return_value=r) as g:
            self.assertEqual(self.app._finnhub_json("quote", "AAPL"), (None, False))
            self.assertEqual(g.call_count, 1)
            self.assertEqual(self.fresh.status()[1]["state"], "paused")
            self.assertEqual(self.app._finnhub_json("quote", "MSFT"), (None, False))
            self.assertEqual(g.call_count, 1)             # second call deferred: no request made

    def test_finnhub_premium_403_does_not_pause(self):
        r = mock.Mock(status_code=403, headers={})
        with mock.patch.object(self.app, "FINNHUB", "tok"), mock.patch.object(self.app.requests, "get", return_value=r):
            self.app._finnhub_json("stock/price-target", "AAPL")
        self.assertEqual(self.fresh.status()[1]["state"], "open")

    def test_calls_are_logged_by_source_and_outer_tag_wins(self):
        fake = {"ok": True, "articles": [], "http": 200, "detail": ""}
        with mock.patch.object(self.app.tavily_client, "configured", return_value=True), \
             mock.patch.object(self.app.tavily_client, "search", return_value=fake):
            self.app._tavily_call("q", 7, 5)
            with self.app._tav_source("chat"):
                self.app._tavily_call("q", 7, 5)
                with self.app._tav_source("brief"):
                    self.app._tavily_call("q", 7, 5)
            self.app._tavily_call("q", 7, 5)
        self.assertEqual(self.fresh.status()[0]["spend"]["by_source"], {"other": 2, "chat": 2})

    def test_scavenger_source_is_batch_paced(self):
        fake = {"ok": True, "articles": [], "http": 200, "detail": ""}
        self.fresh.configure("tavily", batch_daily=1)
        with mock.patch.object(self.app.tavily_client, "configured", return_value=True), \
             mock.patch.object(self.app.tavily_client, "search", return_value=fake) as s:
            with self.app._tav_source("scavenger"):
                self.assertTrue(self.app._tavily_call("q", 7, 5)["ok"])
                r = self.app._tavily_call("q", 7, 5)
        self.assertEqual(s.call_count, 1)
        self.assertTrue(r["deferred"]); self.assertIn("daily soft budget", r["detail"])

    def test_tavily_probe_paygo_logic(self):
        T = self.app.tavily_client
        base = {"ok": True, "http": 200, "plan_usage": 1200, "plan_limit": 1000, "key_usage": 1, "key_limit": None}
        with mock.patch.object(T, "usage", return_value={**base, "paygo": True, "paygo_usage": 1.6, "paygo_limit": 10}):
            self.assertEqual(self.app._tavily_probe()["state"], "ok")       # over the free credits is normal on PAYGO
        with mock.patch.object(T, "usage", return_value={**base, "paygo": True, "paygo_usage": 10, "paygo_limit": 10}):
            self.assertEqual(self.app._tavily_probe()["state"], "quota")    # the account's own PAYGO limit reached
        with mock.patch.object(T, "usage", return_value={**base, "paygo": False, "paygo_usage": None, "paygo_limit": None}):
            self.assertEqual(self.app._tavily_probe()["state"], "quota")

    def test_tavily_probe_reads_usage(self):
        T = self.app.tavily_client
        with mock.patch.object(T, "usage", return_value={"ok": True, "http": 200, "plan_usage": 1000, "plan_limit": 1000,
                                                          "key_usage": 1, "key_limit": None, "paygo": False}):
            self.assertEqual(self.app._tavily_probe()["state"], "quota")
        with mock.patch.object(T, "usage", return_value={"ok": True, "http": 200, "plan_usage": 5, "plan_limit": 1000,
                                                          "key_usage": 1, "key_limit": None, "paygo": False}):
            self.assertEqual(self.app._tavily_probe()["state"], "ok")
        with mock.patch.object(T, "usage", return_value={"ok": True, "http": 200, "plan_usage": None, "plan_limit": None,
                                                          "key_usage": None, "key_limit": None, "paygo": None}):
            self.assertEqual(self.app._tavily_probe()["state"], "ok")
        with mock.patch.object(T, "usage", return_value={"ok": False, "http": 429, "detail": "x"}):
            self.assertEqual(self.app._tavily_probe()["state"], "rate")


if __name__ == "__main__":
    unittest.main()
