# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_scavenger.py -- idea scavenger phase 3 (Jason 2026-11-12): universe (S0), zero-API quant screen (S1) over SEC's
bulk file, ticker-state table. Only a SUCCESSFUL screen may move a ticker's state."""
import datetime, glob, json, os, re, sys, tempfile, unittest, zipfile
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="scav-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import edgar, scavenge_screen, scavenger, store, ticker_state, universe  # noqa: E402

TODAY = datetime.date(2026, 11, 12)
NASDAQ = ("Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
          "GROW|Grow Inc. - Common Stock|Q|N|N|100|N|N\n"
          "BADCO|Bad Co - Common Stock|S|N|D|100|N|N\n"
          "NEWCO|New Co - Common Stock|G|N|N|100|N|N\n"
          "SPYX|Some Index ETF|G|N|N|100|Y|N\n"
          "TESTZ|Test Stock|G|Y|N|100|N|N\n"
          "WARR|Grow Inc. - Warrants|G|N|N|100|N|N\n"
          "NOCIK|Foreign Co - Ordinary Shares|G|N|N|100|N|N\n"
          "File Creation Time: 1112202618:00||||||\n")
OTHER = ("ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
         "BRK.B|Berkshire Hathaway Inc. Class B|N|BRK.B|N|100|N|BRK.B\n"
         "OLDCO|Old Co Common Stock|A|OLDCO|N|100|N|OLDCO\n"
         "PFD|Big Bank 6% Preferred Stock|N|PFD|N|100|N|PFD\n"
         "File Creation Time: 1112202618:00|||||||\n")
SEC = {"fields": ["cik", "name", "ticker", "exchange"],
       "data": [[1, "Grow", "GROW", "Nasdaq"], [2, "Bad", "BADCO", "Nasdaq"], [3, "New", "NEWCO", "Nasdaq"],
                [4, "Berkshire", "BRK-B", "NYSE"], [5, "Old", "OLDCO", "NYSE"], [6, "Warr", "WARR", "Nasdaq"]]}


def _facts(rev, rev_prev, ni, ocf, capex, assets=1000.0, filed="2026-02-20", first="2024-02-20", liab=300.0, eq=700.0,
           shares=100.0, shares_prev=100.0):
    def rows(vals):
        out = []
        for (fy, v, f) in vals:
            out.append({"start": f"{fy}-01-01", "end": f"{fy}-12-31", "val": v, "fy": fy, "fp": "FY", "form": "10-K", "filed": f})
        return out
    ug = {"RevenueFromContractWithCustomerExcludingAssessedTax": rows([(2024, rev_prev, first), (2025, rev, filed)]),
          "NetIncomeLoss": rows([(2024, ni * 0.8, first), (2025, ni, filed)]),
          "NetCashProvidedByUsedInOperatingActivities": rows([(2024, ocf * 0.8, first), (2025, ocf, filed)]),
          "PaymentsToAcquirePropertyPlantAndEquipment": rows([(2024, capex, first), (2025, capex, filed)]),
          "Assets": [{"end": "2024-12-31", "val": assets, "fy": 2024, "fp": "FY", "form": "10-K", "filed": first},
                     {"end": "2025-12-31", "val": assets, "fy": 2025, "fp": "FY", "form": "10-K", "filed": filed}],
          "Liabilities": [{"end": "2025-12-31", "val": liab, "fy": 2025, "fp": "FY", "form": "10-K", "filed": filed}],
          "StockholdersEquity": [{"end": "2025-12-31", "val": eq, "fy": 2025, "fp": "FY", "form": "10-K", "filed": filed}],
          "WeightedAverageNumberOfDilutedSharesOutstanding": rows([(2024, shares_prev, first), (2025, shares, filed)])}
    return {"entityName": "X", "facts": {"us-gaap": {k: {"units": {"USD": v}} for k, v in ug.items()}}}


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for cik, facts in files.items():
            z.writestr(f"CIK{cik:010d}.json", json.dumps(facts))


class TestUniverse(unittest.TestCase):
    def setUp(self):
        self.rows, self.fun = universe.build_universe(NASDAQ, OTHER, SEC)

    def test_keeps_common_stocks_only_and_counts_the_rest(self):
        self.assertEqual({r["ticker"] for r in self.rows}, {"GROW", "BADCO", "NEWCO", "NOCIK", "BRK-B", "OLDCO"})
        f = self.fun
        self.assertEqual((f["listed"], f["etfs"], f["test_issues"], f["not_common"], f["stocks"]), (10, 1, 1, 2, 6))
        self.assertEqual((f["with_cik"], f["no_cik"]), (5, 1))

    def test_share_class_symbol_joins_to_sec_dash_form(self):
        by = {r["ticker"]: r for r in self.rows}
        self.assertEqual(by["BRK-B"]["cik"], "0000000004")
        self.assertEqual(by["BRK-B"]["exchange"], "NYSE")
        self.assertEqual(by["BADCO"]["fin_status"], "D")
        self.assertIsNone(by["NOCIK"]["cik"])

    def test_fetch_falls_back_to_saved_copy_and_flags_it_stale(self):
        d = tempfile.mkdtemp(dir=_TMP)
        R = lambda txt: mock.Mock(status_code=200, content=txt.encode() * 2000, text=txt)
        src, stale = universe.fetch_sources(lambda u: R(json.dumps(SEC)), lambda u: R(NASDAQ if "nasdaqlisted" in u else OTHER), d)
        self.assertEqual(stale, [])
        boom = mock.Mock(side_effect=OSError("down"))
        src2, stale2 = universe.fetch_sources(boom, boom, d)
        self.assertEqual(set(stale2), {"nasdaq", "other", "sec"})
        self.assertEqual(src2["nasdaq"], src["nasdaq"])
        with self.assertRaises(RuntimeError):
            universe.fetch_sources(boom, boom, tempfile.mkdtemp(dir=_TMP))


class TestClassify(unittest.TestCase):
    def _s1(self, **kw):
        base = {"revenue": 5e7, "rev_growth_pct": 25.0, "fcf_margin_pct": 8.0, "net_margin_pct": 5.0, "debt_to_equity": 0.5,
                "share_change_pct": 1.0, "piotroski": 7, "altman_zone": "safe", "first_filed": "2015-03-01", "last_filed": "2026-10-01"}
        base.update(kw)
        return base

    def c(self, s1=None, status="ok", **row):
        return scavenge_screen.classify({"fin_status": "N", **row}, s1, TODAY, status)

    def test_active_when_nothing_is_wrong(self):
        r = self.c(self._s1())
        self.assertEqual((r["state"], r["reason_kind"], r["next_check"]), ("active", "screened", None))

    def test_exchange_flag_is_blacklisted_with_a_recheck_date(self):
        r = self.c(self._s1(), fin_status="D")
        self.assertEqual((r["state"], r["reason_kind"], r["next_check"]), ("blacklist", "exchange_flag", "2027-02-10"))

    def test_missing_data_cases_are_reconsider_not_blacklist(self):
        for status, kind in (("no_cik", "no_sec_cik"), ("no_facts", "no_financials"), ("no_gaap", "not_us_gaap")):
            r = self.c(None, status)
            self.assertEqual((r["state"], r["reason_kind"]), ("reconsider", kind))

    def test_dormant_distressed_shrinking_pre_revenue_and_recent_ipo(self):
        self.assertEqual(self.c(self._s1(last_filed="2024-01-01"))["reason_kind"], "dormant")
        self.assertEqual(self.c(self._s1(altman_zone="distress", rev_growth_pct=-5, fcf_margin_pct=-10))["reason_kind"], "distressed")
        self.assertEqual(self.c(self._s1(rev_growth_pct=-30, net_margin_pct=-5))["reason_kind"], "shrinking")
        self.assertEqual(self.c(self._s1(revenue=100))["reason_kind"], "pre_revenue")
        r = self.c(self._s1(first_filed="2026-03-01"))
        self.assertEqual((r["state"], r["reason_kind"], r["next_check"]), ("new", "recent_ipo", "2026-12-12"))

    def test_flags_count_passes_and_ignore_missing(self):
        f = scavenge_screen.flags(self._s1(fcf_margin_pct=None))
        self.assertIsNone(f["cash_positive"])
        self.assertEqual(f["score"], 5)


class TestScreenAndJob(unittest.TestCase):
    def setUp(self):
        store.init_db()
        self.d = tempfile.mkdtemp(dir=_TMP)
        self.zip = os.path.join(self.d, "cf.zip")
        _zip(self.zip, {1: _facts(200e6, 100e6, 30e6, 40e6, 5e6, assets=1e9, liab=3e8, eq=7e8),                                  # GROW: doubling, cash positive
                        3: _facts(50e6, 30e6, 2e6, 3e6, 1e6, first="2026-03-01", filed="2026-09-01"),   # NEWCO: recent IPO
                        4: _facts(9e9, 8e9, 1e9, 1.4e9, 2e8),                              # BRK-B
                        5: _facts(5e7, 5e7, 1, 1, 1, filed="2023-01-01"),                  # OLDCO: dormant
                        99: _facts(1, 1, 1, 1, 1)})                                        # not in the universe
        self.rows, _ = universe.build_universe(NASDAQ, OTHER, SEC)

    def test_screen_zip_classifies_every_row_from_the_file_alone(self):
        out, stats = scavenge_screen.screen_zip(self.zip, self.rows, TODAY)
        by = {r["ticker"]: r for r in out}
        self.assertEqual((stats["companies_in_file"], stats["matched"], stats["parse_errors"]), (5, 4, 0))
        self.assertEqual(by["GROW"]["state"], "active")
        self.assertAlmostEqual(by["GROW"]["s1"]["rev_growth_pct"], 100.0, places=1)
        self.assertGreaterEqual(by["GROW"]["scores"]["score"], 4)
        self.assertEqual(by["NEWCO"]["state"], "new")
        self.assertEqual(by["OLDCO"]["reason_kind"], "dormant")
        self.assertEqual(by["BADCO"]["reason_kind"], "exchange_flag")        # flagged listing wins even with no facts
        self.assertEqual(by["NOCIK"]["reason_kind"], "no_sec_cik")

    def test_unreadable_company_is_left_alone_not_downgraded(self):
        bad = os.path.join(self.d, "bad.zip")
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("CIK0000000001.json", "{not json")
        out, stats = scavenge_screen.screen_zip(bad, self.rows, TODAY)
        self.assertEqual(stats["parse_errors"], 1)
        self.assertNotIn("GROW", {r["ticker"] for r in out})

    def test_state_table_only_changes_state_on_a_real_change(self):
        ticker_state.ensure()
        with store._conn() as c:
            c.execute("DELETE FROM ticker_state")
        out, _ = scavenge_screen.screen_zip(self.zip, self.rows, TODAY)
        first = ticker_state.upsert_many(out, "2026-11-12")
        self.assertEqual(first["added"], len(out))
        again = ticker_state.upsert_many(out, "2026-11-13")
        self.assertEqual((again["added"], again["changed"], again["unchanged"]), (0, 0, len(out)))
        row = ticker_state.list_rows(q="GROW")[0]
        self.assertEqual((row["first_seen"], row["last_seen"], row["last_changed"]), ("2026-11-12", "2026-11-13", "2026-11-12"))
        out2 = [dict(r, state="blacklist", reason_kind="x") if r["ticker"] == "GROW" else r for r in out]
        self.assertEqual(ticker_state.upsert_many(out2, "2026-11-14")["changed"], 1)
        self.assertEqual(ticker_state.list_rows(q="GROW")[0]["last_changed"], "2026-11-14")
        self.assertEqual(sum(ticker_state.counts().values()), len(out))

    def test_job_downloads_screens_deletes_zip_and_skips_when_unchanged(self):
        ticker_state.ensure()
        with store._conn() as c:
            c.execute("DELETE FROM ticker_state")
        src = {"nasdaq": NASDAQ, "other": OTHER, "sec": json.dumps(SEC)}
        calls = []

        def dl(dest, seen, force):
            calls.append((seen, force))
            if seen == "LM1" and not force:
                return None, "LM1"
            import shutil
            shutil.copy(self.zip, dest)
            return dest, "LM1"
        meta = os.path.join(self.d, "meta.json")
        with mock.patch.object(scavenger, "META_FILE", meta), mock.patch.object(scavenger, "UNIVERSE_DIR", self.d), \
             mock.patch.object(universe, "fetch_sources", return_value=(src, [])):
            s1 = scavenger.run_universe(today=TODAY, download=dl)
            self.assertTrue(s1["screened"])
            self.assertFalse(os.path.exists(os.path.join(self.d, "companyfacts.zip")))     # 1.4 GB file does not linger
            self.assertEqual(sum(s1["counts"].values()), 6)
            s2 = scavenger.run_universe(today=TODAY, download=dl)
            self.assertFalse(s2["screened"])
            self.assertIn("not changed", s2["note"])
            self.assertEqual(scavenger.read_meta()["last_screen"]["date"], "2026-11-12")  # last real screen is kept
            s3 = scavenger.run_universe(force=True, today=TODAY, download=dl)
            self.assertTrue(s3["screened"])
        self.assertEqual([c[1] for c in calls], [True, False, True])   # first run: no rules version saved yet, so it is forced

    def test_failed_download_leaves_states_untouched(self):
        ticker_state.ensure()
        before = ticker_state.counts()
        src = {"nasdaq": NASDAQ, "other": OTHER, "sec": json.dumps(SEC)}
        with mock.patch.object(universe, "fetch_sources", return_value=(src, [])), \
             mock.patch.object(scavenger, "META_FILE", os.path.join(self.d, "m2.json")):
            with self.assertRaises(RuntimeError):
                scavenger.run_universe(today=TODAY, download=mock.Mock(side_effect=RuntimeError("HTTP 503")))
        self.assertEqual(ticker_state.counts(), before)


def _ifrs(rev, rev_prev, ni, ocf, capex, assets=1000.0, liab=300.0, eq=700.0, filed="2026-04-01", cur="CAD"):
    def flow(vals):
        return {"units": {cur: [{"start": f"{fy}-01-01", "end": f"{fy}-12-31", "val": v, "fy": fy, "fp": "FY", "form": "40-F", "filed": f}
                                for fy, v, f in vals]}}

    def inst(v):
        return {"units": {cur: [{"end": "2023-12-31", "val": v, "fy": 2023, "fp": "FY", "form": "40-F", "filed": "2024-04-01"},
                                {"end": "2025-12-31", "val": v, "fy": 2025, "fp": "FY", "form": "40-F", "filed": filed}]}}
    return {"entityName": "Foreign Co", "facts": {"ifrs-full": {
        "Revenue": flow([(2024, rev_prev, "2025-04-01"), (2025, rev, filed)]), "ProfitLoss": flow([(2025, ni, filed)]),
        "CashFlowsFromUsedInOperatingActivities": flow([(2025, ocf, filed)]),
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": flow([(2025, capex, filed)]),
        "Assets": inst(assets), "Liabilities": inst(liab), "Equity": inst(eq)}}}


class TestScreenFixes(unittest.TestCase):
    """Cowork RESULT-018 findings F1-F3 plus the IFRS path for Jason's foreign picks."""
    def setUp(self):
        store.init_db()
        self.d = tempfile.mkdtemp(dir=_TMP)

    def _screen(self, tickers_facts):
        path = os.path.join(self.d, "z.zip")
        _zip(path, {i + 1: f for i, (t, f) in enumerate(tickers_facts)})
        rows = [{"ticker": t, "name": t, "exchange": "NYSE", "cik": str(i + 1).zfill(10), "fin_status": "N"}
                for i, (t, f) in enumerate(tickers_facts)]
        out, stats = scavenge_screen.screen_zip(path, rows, TODAY)
        return {r["ticker"]: r for r in out}, stats

    def test_f1_foreign_filer_that_switched_to_ifrs_is_not_dormant(self):
        old_gaap = _facts(5e9, 4e9, 1e8, 2e8, 5e7, filed="2014-08-11", first="2012-08-11")        # last us-gaap filing 2014
        old_gaap["facts"]["ifrs-full"] = _ifrs(6e9, 5e9, 9e8, 1.5e9, 4e8)["facts"]["ifrs-full"]    # recent IFRS filings
        by, stats = self._screen([("AEM", old_gaap)])
        self.assertNotEqual(by["AEM"]["reason_kind"], "dormant")
        self.assertEqual(by["AEM"]["s1"]["basis"], "ifrs")
        self.assertEqual(by["AEM"]["state"], "active")

    def test_truly_dormant_us_filer_is_still_dormant(self):
        by, _ = self._screen([("OLD", _facts(5e7, 5e7, 1, 1, 1, filed="2023-01-01"))])
        self.assertEqual(by["OLD"]["reason_kind"], "dormant")

    def test_ifrs_only_filer_gets_real_numbers_not_not_us_gaap(self):
        by, stats = self._screen([("DPRO", _ifrs(8e6, 4e6, -2e6, -1e6, 5e5, cur="USD"))])
        r = by["DPRO"]
        self.assertEqual(r["state"], "active")
        self.assertEqual((r["s1"]["basis"], r["s1"]["currency"]), ("ifrs", "USD"))
        self.assertAlmostEqual(r["s1"]["rev_growth_pct"], 100.0, places=1)
        self.assertAlmostEqual(r["s1"]["net_margin_pct"], -25.0, places=1)
        self.assertAlmostEqual(r["s1"]["fcf_margin_pct"], -18.75, places=1)
        self.assertEqual(stats["ifrs_read"], 1)

    def test_ifrs_filer_without_revenue_stays_reconsider(self):
        f = _ifrs(1, 1, 1, 1, 1)
        del f["facts"]["ifrs-full"]["Revenue"]
        by, _ = self._screen([("CRML", f)])
        self.assertEqual((by["CRML"]["state"], by["CRML"]["reason_kind"]), ("reconsider", "pre_revenue"))

    def test_f3_bank_with_interest_income_revenue_is_not_pre_revenue(self):
        bank = _facts(0, 0, 3e8, 4e8, 1e7, assets=2e10, liab=1.8e10, eq=2e9)
        ug = bank["facts"]["us-gaap"]
        del ug["RevenueFromContractWithCustomerExcludingAssessedTax"]
        ug["InterestAndDividendIncomeOperating"] = {"units": {"USD": [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 8e8, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-20"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 1e9, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-20"}]}}
        by, _ = self._screen([("BANK", bank)])
        self.assertEqual(by["BANK"]["state"], "active")
        self.assertTrue(by["BANK"]["s1"]["revenue_alt_tags"])
        self.assertAlmostEqual(by["BANK"]["s1"]["rev_growth_pct"], 25.0, places=1)
        self.assertAlmostEqual(by["BANK"]["s1"]["net_margin_pct"], 30.0, places=1)

    def test_f2_liquid_cash_burner_is_not_blacklisted_but_thin_liquidity_is(self):
        base = {"revenue": 3e8, "rev_growth_pct": -4.0, "fcf_margin_pct": -30.0, "net_margin_pct": -40.0,
                "altman_zone": "distress", "first_filed": "2015-01-01", "last_filed": "2026-10-01"}
        liquid = scavenge_screen.classify({"fin_status": "N"}, {**base, "current_ratio": 6.0}, TODAY, "ok")
        thin = scavenge_screen.classify({"fin_status": "N"}, {**base, "current_ratio": 0.8}, TODAY, "ok")
        unknown = scavenge_screen.classify({"fin_status": "N"}, {**base, "current_ratio": None}, TODAY, "ok")
        self.assertEqual((liquid["state"], liquid["reason_kind"]), ("reconsider", "burning_cash_liquid"))
        self.assertEqual((thin["state"], thin["reason_kind"]), ("blacklist", "distressed"))
        self.assertEqual(unknown["state"], "blacklist")

    def test_rules_change_forces_one_rescreen_and_job_runs_in_batch_mode(self):
        zp = os.path.join(self.d, "cf.zip")
        _zip(zp, {1: _facts(200e6, 100e6, 30e6, 40e6, 5e6, assets=1e9, liab=3e8, eq=7e8)})
        src = {"nasdaq": NASDAQ, "other": OTHER, "sec": json.dumps(SEC)}
        seen = []

        def dl(dest, last, force):
            seen.append((bool(getattr(edgar._MODE, "batch", False)), force))
            import shutil
            shutil.copy(zp, dest)
            return dest, "LM9"
        meta = os.path.join(self.d, "meta.json")
        store.write_json(meta, {"facts_last_modified": "LM9", "screen_version": scavenge_screen.SCREEN_VERSION - 1})
        with mock.patch.object(scavenger, "META_FILE", meta), mock.patch.object(scavenger, "UNIVERSE_DIR", self.d), \
             mock.patch.object(universe, "fetch_sources", return_value=(src, [])):
            out = scavenger.run_universe(today=TODAY, download=dl)
            self.assertEqual(scavenger.read_meta()["screen_version"], scavenge_screen.SCREEN_VERSION)
        self.assertTrue(out["screened"])
        self.assertEqual(seen, [(True, True)])                       # batch pace, and forced by the old rules version


class TestFollowups(unittest.TestCase):
    """Cowork RESULT-019: foreign annual filers called dormant (NEW-1), ABR picked a sub-line as revenue, AEG's IFRS 17 revenue
    tag, CRML reports no annual revenue, and a /health build id that moves with the code."""
    def setUp(self):
        store.init_db()
        self.d = tempfile.mkdtemp(dir=_TMP)

    def _screen(self, tickers_facts):
        path = os.path.join(self.d, "z.zip")
        _zip(path, {i + 1: f for i, (t, f) in enumerate(tickers_facts)})
        rows = [{"ticker": t, "name": t, "exchange": "NYSE", "cik": str(i + 1).zfill(10), "fin_status": "N"}
                for i, (t, f) in enumerate(tickers_facts)]
        out, stats = scavenge_screen.screen_zip(path, rows, TODAY)
        return {r["ticker"]: r for r in out}

    def test_foreign_filer_with_old_financials_and_nothing_newer_is_reconsider_not_hidden(self):
        nu = _ifrs(1e10, 8e9, 1e9, 2e9, 1e8, filed="2025-04-16")
        by = self._screen([("NU", nu)])
        self.assertEqual((by["NU"]["state"], by["NU"]["reason_kind"]), ("reconsider", "stale_foreign"))
        self.assertTrue(by["NU"]["s1"]["financials_stale"])

    def test_foreign_filer_whose_recent_filing_shows_up_only_as_cover_page_data_is_not_stale(self):
        pbr = _ifrs(1e10, 8e9, 1e9, 2e9, 1e8, filed="2025-04-03")
        pbr["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2025-12-31", "val": 1, "form": "20-F", "filed": "2026-04-09"}]}}}
        by = self._screen([("PBR", pbr)])
        self.assertEqual(by["PBR"]["state"], "active")
        self.assertEqual(by["PBR"]["s1"]["last_filed_any"], "2026-04-09")

    def test_us_company_with_no_recent_filing_of_any_kind_is_still_dormant(self):
        by = self._screen([("OLD", _facts(5e7, 5e7, 1, 1, 1, filed="2023-01-01"))])
        self.assertEqual((by["OLD"]["state"], by["OLD"]["reason_kind"]), ("blacklist", "dormant"))

    def test_abr_style_reit_takes_total_interest_income_not_a_small_sub_line(self):
        f = _facts(0, 0, 1.07e8, 3e8, 1e7, assets=1e10, liab=8e9, eq=2e9)
        ug = f["facts"]["us-gaap"]
        del ug["RevenueFromContractWithCustomerExcludingAssessedTax"]

        def fy(v25, v24):
            return {"units": {"USD": [
                {"start": "2024-01-01", "end": "2024-12-31", "val": v24, "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-20"},
                {"start": "2025-01-01", "end": "2025-12-31", "val": v25, "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-20"}]}}
        ug["InterestIncomeOperating"] = fy(940e6, 1000e6)
        ug["RealEstateRevenueNet"] = fy(11e6, 12e6)
        by = self._screen([("ABR", f)])
        self.assertEqual(by["ABR"]["s1"]["revenue"], 940e6)
        self.assertAlmostEqual(by["ABR"]["s1"]["rev_growth_pct"], -6.0, places=1)
        self.assertLess(abs(by["ABR"]["s1"]["net_margin_pct"]), 20)

    def test_aeg_style_insurer_follows_the_concept_that_reaches_the_newest_year(self):
        f = _ifrs(1, 1, 5e8, 1e9, 1e7)
        ifrs = f["facts"]["ifrs-full"]
        ifrs["Revenue"] = {"units": {"EUR": [{"start": "2022-01-01", "end": "2022-12-31", "val": 21e9, "form": "20-F", "filed": "2023-03-22"}]}}
        ifrs["InsuranceRevenue"] = {"units": {"EUR": [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 9.8e9, "form": "20-F", "filed": "2025-03-27"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 9.1e9, "form": "20-F", "filed": "2026-03-26"}]}}
        by = self._screen([("AEG", f)])
        self.assertEqual((by["AEG"]["s1"]["revenue"], by["AEG"]["s1"]["currency"]), (9.1e9, "EUR"))
        self.assertEqual(by["AEG"]["state"], "active")

    def test_crml_style_explorer_with_only_half_year_revenue_is_pre_revenue_with_a_runway(self):
        f = _ifrs(1, 1, -3e7, -2e7, 5e6)
        f["facts"]["ifrs-full"]["Revenue"] = {"units": {"USD": [
            {"start": "2024-07-01", "end": "2024-12-31", "val": 359859, "form": "6-K", "filed": "2025-03-01"}]}}
        f["facts"]["ifrs-full"]["CashAndCashEquivalents"] = {"units": {"USD": [
            {"end": "2025-12-31", "val": 5e7, "form": "20-F", "filed": "2026-04-01"}]}}
        by = self._screen([("CRML", f)])
        r = by["CRML"]
        self.assertEqual((r["state"], r["reason_kind"]), ("reconsider", "pre_revenue"))
        self.assertIsNone(r["s1"]["revenue"])
        self.assertEqual(r["s1"]["runway_months"], 24.0)             # cash 50M, burn (-20M - 5M capex) ... 50 / 25 * 12
        self.assertTrue(r["s1"]["foreign_filer"])

    def test_runway_from_fcf(self):
        self.assertEqual(scavenge_screen.runway_from_fcf(100.0, -50.0), 24.0)
        self.assertEqual(scavenge_screen.runway_from_fcf(100.0, 5.0), 999)
        self.assertIsNone(scavenge_screen.runway_from_fcf(None, -5.0))

    def test_health_reports_a_code_build_that_is_a_hash_of_the_code(self):
        import app as appmod
        out = appmod.health()
        self.assertRegex(out["code_build"], r"^[0-9a-f]{10}$")
        self.assertEqual(out["code_build"], appmod._code_build())


class TestWiring(unittest.TestCase):
    def test_job_schedule_endpoints_and_page(self):
        with open(os.path.join(_HERE, "app.py"), encoding="utf-8") as f:
            src = f.read()
        for needle in ('"universe":      ("Universe & first screen"', "/api/run-universe", "/api/universe/tickers",
                       '_SCHEDULE_ORDER = ["universe",', '"hour": 3, "minute": 0'):
            self.assertIn(needle, src, needle)
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            self.assertIn("universe: '/api/run-universe'", f.read())

    def test_metrics_refactor_matches_fundamentals_path(self):
        facts = _facts(200, 100, 30, 40, 5)
        res = edgar._metrics_from_facts("GROW", "0000000001", facts)
        self.assertTrue(res["available"])
        self.assertAlmostEqual(res["metrics"]["revenue_growth_pct"], 100.0, places=1)


if __name__ == "__main__":
    unittest.main()
