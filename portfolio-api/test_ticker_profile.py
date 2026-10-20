# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_ticker_profile.py -- HOLDINGS editor ticker auto-fill (tester request 2026-10-20)."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="tp-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import edgar  # noqa: E402

EX = {"AAPL": {"name": "Apple Inc.", "exchange": "Nasdaq"},
      "SPY": {"name": "SPDR S&P 500 ETF TRUST", "exchange": "NYSE"},
      "VOYG": {"name": "Voyager Technologies, Inc./TX", "exchange": "NYSE"}}
MF = {"YYY": {"cik": "1", "seriesId": "S1"}, "VFIAX": {"cik": "2", "seriesId": "S2"}, "SPY": {"cik": "3", "seriesId": "S3"}}


def prof(t, series_name=None, ex=EX, mf=MF, cik=None):
    with mock.patch.object(edgar, "_exchange_map", lambda: ex), \
         mock.patch.object(edgar, "fund_series", lambda x: mf.get(x)), \
         mock.patch.object(edgar, "fund_holdings", lambda x, top=600: {"series_name": series_name}), \
         mock.patch.object(edgar, "get_cik", lambda x: cik):
        return edgar.ticker_profile(t)


class TestTickerProfile(unittest.TestCase):
    def test_stock(self):
        p = prof("aapl")
        self.assertEqual((p["found"], p["type"], p["name"]), (True, "Stock", "Apple Inc."))

    def test_unlisted_fund_ticker_is_an_etf_with_series_name(self):          # the tester's YYY example
        p = prof("YYY", series_name="Amplify CEF High Income ETF")
        self.assertEqual((p["type"], p["name"]), ("ETF", "Amplify CEF High Income ETF"))

    def test_exchange_listed_fund_is_etf(self):
        self.assertEqual(prof("SPY", series_name="SPDR S&P 500 ETF Trust")["type"], "ETF")

    def test_mutual_fund_ticker(self):
        p = prof("VFIAX", series_name="Vanguard 500 Index Fund")
        self.assertEqual(p["type"], "Mutual Fund")

    def test_voyg_is_the_sec_registrant_not_the_old_crypto_broker(self):
        self.assertIn("Voyager Technologies", prof("VOYG")["name"])

    def test_unknown_and_bad_tickers(self):
        self.assertFalse(prof("ZZZZQ")["found"])
        self.assertFalse(prof("BTC USD")["found"])
        self.assertFalse(prof("")["found"])

    def test_sec_outage_never_raises(self):
        def boom():
            raise edgar.CikMapUnavailable("down")
        with mock.patch.object(edgar, "fund_series", lambda x: None), mock.patch.object(edgar, "_exchange_map", boom):
            p = edgar.ticker_profile("AAPL")
        self.assertFalse(p["found"]); self.assertIn("unavailable", p["error"])


class TestNameCase(unittest.TestCase):
    def test_all_caps_fund_names_are_title_cased_mixed_case_untouched(self):
        import app as appmod
        self.assertEqual(appmod._readable_fund_name("VANGUARD 500 INDEX FUND"), "Vanguard 500 Index Fund")
        self.assertEqual(appmod._readable_fund_name("SPDR S&P 500 ETF TRUST"), "SPDR S&P 500 ETF Trust")
        self.assertEqual(appmod._readable_fund_name("Amplify CEF High Income ETF"), "Amplify CEF High Income ETF")


class TestEndpointAndUi(unittest.TestCase):
    HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
    SRC = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()

    def test_endpoint_is_admin_only(self):
        i = self.SRC.index("def get_ticker_profile")
        self.assertIn("_require_admin(request)", self.SRC[i:i + 400])

    def test_ui_wires_lookup_on_change_and_keeps_user_typed_names(self):
        for needle in ("/api/ticker-profile?ticker=", "inp.onchange=()=>lookupTicker", "nm.dataset.auto==='1'",
                       "ty.dataset.touched!=='1'", "hold-inst-list"):
            self.assertIn(needle, self.HTML)


if __name__ == "__main__":
    unittest.main()
