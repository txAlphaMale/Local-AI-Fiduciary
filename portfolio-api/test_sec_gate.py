# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_sec_gate.py -- SEC EDGAR requests honor the API budget gate (Jason 2026-11-12): a 403/429 pauses SEC with a
back-off, a paused SEC raises SecDeferred (never 'no data'), recovery is by a single trial call, 404 is healthy."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="secgate-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import budget  # noqa: E402
import edgar  # noqa: E402


class Clock:
    def __init__(self): self.t = 1_800_000_000.0
    def __call__(self): return self.t


class Resp:
    def __init__(self, code, headers=None): self.status_code, self.headers = code, headers or {}


class TestSecGate(unittest.TestCase):
    def setUp(self):
        self.clk = Clock()
        self.b = budget.Budget(clock=self.clk, load=lambda: None, save=lambda d: None)
        edgar.set_gate(self.b)
        self.addCleanup(edgar.set_gate, None)

    def _get(self, code, headers=None):
        with mock.patch.object(edgar.requests, "get", return_value=Resp(code, headers)) as g:
            try:
                return edgar._get("https://data.sec.gov/x"), g
            except edgar.SecDeferred as e:
                return e, g

    def state(self):
        return {r["provider"]: r for r in self.b.status()}["sec"]

    def test_no_gate_is_plain_get(self):
        edgar.set_gate(None)
        r, g = self._get(200)
        self.assertEqual(r.status_code, 200); g.assert_called_once()

    def test_ok_and_404_stay_open(self):
        for code in (200, 404):
            r, _ = self._get(code)
            self.assertEqual(r.status_code, code)
        self.assertEqual(self.state()["state"], "open")

    def test_403_pauses_and_next_call_is_deferred_without_hitting_sec(self):
        self._get(403)
        st = self.state()
        self.assertEqual((st["state"], st["kind"]), ("paused", "rate"))
        e, g = self._get(200)
        self.assertIsInstance(e, edgar.SecDeferred)
        g.assert_not_called()

    def test_retry_after_is_honored_and_429_pauses(self):
        self._get(429, {"Retry-After": "600"})
        self.assertGreaterEqual(self.state()["paused_until"] - self.clk.t, 600)

    def test_recovers_after_backoff_with_one_trial_call(self):
        self._get(403)
        self.clk.t += 3700
        r, g = self._get(200)
        self.assertEqual(r.status_code, 200); g.assert_called_once()
        self.assertEqual(self.state()["state"], "open")

    def test_deferred_is_a_connection_error_so_callers_treat_it_as_unavailable(self):
        self.assertTrue(issubclass(edgar.SecDeferred, edgar.requests.ConnectionError))

    def test_fundamentals_reports_a_fetch_error_not_no_data_when_paused(self):
        self._get(403)
        with mock.patch.object(edgar, "get_cik", return_value="0000000001"):
            out = edgar._compute("ABC")
        self.assertTrue(out["error"]); self.assertFalse(out["available"])
        self.assertIn("deferred", out["reason"])

    def test_app_attaches_the_shared_gate(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn("edgar.set_gate(_budget)", src)

    def test_no_raw_requests_get_left_in_edgar(self):
        src = open(os.path.join(_HERE, "edgar.py"), encoding="utf-8").read()
        self.assertEqual(src.count("requests.get("), 2)   # the wrapper's own two calls


if __name__ == "__main__":
    unittest.main()
