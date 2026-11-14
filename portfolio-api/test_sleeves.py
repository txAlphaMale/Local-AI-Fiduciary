# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_sleeves.py -- idea-scavenger sleeves, phase 4a (Jason 2026-11-12): rules from a fixed metric menu evaluated over the
SEC-data screen; built-ins Growth / Income / Profitable compounder / Balance; custom sleeves with a preview count."""
import asyncio, datetime, glob, json, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="sleeves-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod, scavenge_screen as scr, sleeves, store, ticker_state  # noqa: E402

TODAY = datetime.date(2026, 11, 12)


def row(t, state="active", score=4, **s1):
    base = {"revenue": 5e7, "rev_growth_pct": 30.0, "net_margin_pct": -5.0, "fcf_margin_pct": -10.0, "runway_months": 30.0,
            "share_change_pct": 5.0, "debt_to_equity": 0.5, "piotroski": 5, "first_filed": "2021-01-01"}
    base.update(s1)
    return {"ticker": t, "name": t + " Inc", "exchange": "NYSE", "state": state, "reason_kind": "screened",
            "s1": base, "scores": {"score": score}}


class TestEngine(unittest.TestCase):
    def test_growth_needs_no_profit_but_needs_runway_and_limited_dilution(self):
        rows = [row("RKT"), row("BURN", runway_months=4.0), row("DIL", share_change_pct=3000.0), row("SLOW", rev_growth_pct=8.0),
                row("UNK", runway_months=None)]
        ev = sleeves.evaluate({**sleeves.BUILTIN["growth"]}, rows, TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["RKT"])
        self.assertEqual(ev["considered"], 5)

    def test_unknown_value_fails_the_rule_and_ranking_orders_matches(self):
        rows = [row("A", rev_growth_pct=40.0), row("B", rev_growth_pct=90.0), row("C", rev_growth_pct=60.0)]
        ev = sleeves.evaluate(sleeves.BUILTIN["growth"], rows, TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["B", "C", "A"])

    def test_blacklist_and_rows_without_financials_are_never_offered(self):
        rows = [row("BL", state="blacklist"), {**row("NOFIN"), "s1": None}, row("OK", state="reconsider")]
        ev = sleeves.evaluate(sleeves.BUILTIN["growth"], rows, TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["OK"])

    def test_market_data_rules_apply_once_data_exists_and_otherwise_wait(self):
        sl = {**sleeves.BUILTIN["growth"], "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 25},
                                                      {"metric": "market_cap", "op": "<=", "value": 5e9}]}
        small = {**row("SMALL"), "s2": {"available": True, "market_cap": 2e9}}
        big = {**row("BIG"), "s2": {"available": True, "market_cap": 9e10}}
        nodata = {**row("NODATA"), "s2": {"available": False}}
        ev = sleeves.evaluate(sl, [small, big, nodata, row("NEW")], TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["SMALL"])
        self.assertEqual(ev["awaiting"], 1)                       # NEW passes the SEC rules but has no market data yet

    def test_short_interest_rules_stay_pending_until_some_name_has_finra_data(self):
        sl = {**sleeves.BUILTIN["growth"], "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 25},
                                                      {"metric": "short_days_to_cover", "op": "<=", "value": 5}]}
        ev = sleeves.evaluate(sl, [row("X")], TODAY)
        self.assertEqual(len(ev["matches"]), 1)
        self.assertIn("needs FINRA", ev["pending_rules"][0])
        have = {**row("Y"), "s3": {"short": {"available": True, "days_to_cover": 3.0}}}
        ev2 = sleeves.evaluate(sl, [have, row("Z")], TODAY)
        self.assertEqual([r["ticker"] for r in ev2["matches"]], ["Y"])       # now a real rule: Z has no short data yet
        self.assertEqual((ev2["awaiting"], ev2["pending_rules"]), (1, []))

    def test_signals_rules_apply_once_data_exists_and_otherwise_wait(self):
        sl = sleeves.BUILTIN["insiders"]
        buying = {**row("BUY"), "s3": {"insider": {"available": True, "net_value": 250000.0, "buyers": 3}}}
        one = {**row("ONE"), "s3": {"insider": {"available": True, "net_value": 60000.0, "buyers": 1}}}
        none = {**row("NONE"), "s3": {"insider": {"available": False}}}
        ev = sleeves.evaluate(sl, [buying, one, none, row("NEW")], TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["BUY"])
        self.assertEqual(ev["awaiting"], 1)

    def test_analyst_metrics_read_the_signals_record(self):
        r = {**row("A"), "s3": {"analyst": {"available": True, "buy_pct": 80.0, "total": 12}}}
        self.assertEqual((sleeves.value_of(r, "analyst_buy_pct"), sleeves.value_of(r, "analyst_count")), (80.0, 12))
        self.assertIsNone(sleeves.value_of(row("B"), "analyst_buy_pct"))

    def test_balance_sleeve_matches_healthy_names_in_sectors_you_are_light_on(self):
        bal = sleeves.BUILTIN["balance"]
        mk = lambda t, sec, score: {**row(t, score=score), "s2": {"available": True, "sector": sec}}
        rows = [mk("TECH1", "Technology", 6), mk("HEAL1", "Health Care", 6), mk("HEAL2", "Health Care", 3), row("NOS2", score=6)]
        ev = sleeves.evaluate(bal, rows, TODAY, held={"Technology": 40.0, "Health Care": 1.0})
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["HEAL1"])      # TECH1 is not a gap; HEAL2 is weak; NOS2 waits
        self.assertEqual(ev["awaiting"], 1)
        self.assertEqual(ev["gap_sectors"], [("Health Care", 1)])

    def test_income_and_compounder_builtins(self):
        pay = row("DIV", net_margin_pct=12.0, fcf_margin_pct=9.0, dividend_payout_pct=45.0, fcf_dividend_coverage=2.5)
        nodiv = row("NODIV", net_margin_pct=12.0, dividend_payout_pct=None, fcf_dividend_coverage=None)
        self.assertEqual([r["ticker"] for r in sleeves.evaluate(sleeves.BUILTIN["income"], [pay, nodiv], TODAY)["matches"]], ["DIV"])
        comp = row("CMP", net_margin_pct=18.0, fcf_margin_pct=15.0, piotroski=8, share_change_pct=-1.0, rev_growth_pct=14.0)
        self.assertEqual([r["ticker"] for r in sleeves.evaluate(sleeves.BUILTIN["compounder"], [comp, row("LOSS")], TODAY)["matches"]], ["CMP"])

    def test_age_years_and_score_metrics(self):
        r = row("A", first_filed="2021-11-12")
        self.assertEqual(sleeves.value_of(r, "age_years", TODAY), 5.0)
        self.assertEqual(sleeves.value_of(r, "s1_score"), 4)
        self.assertIsNone(sleeves.value_of(r, "market_cap"))

    def test_builtins_all_validate(self):
        for sid, sl in sleeves.BUILTIN.items():
            clean, err = sleeves.validate(sl)
            self.assertIsNone(err, sid)


class TestValidate(unittest.TestCase):
    def v(self, **kw):
        base = {"name": "Mine", "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 10}]}
        base.update(kw)
        return sleeves.validate(base)

    def test_good_sleeve_is_cleaned(self):
        clean, err = self.v(intent="x" * 500, notes="look for <script>", junk="dropped")
        self.assertIsNone(err)
        self.assertEqual(len(clean["intent"]), 300)
        self.assertNotIn("junk", clean)
        self.assertEqual(clean["rank_by"], "s1_score")

    def test_bad_inputs_are_refused_with_a_reason(self):
        for kw, frag in ((dict(name=""), "name"), (dict(rules=[{"metric": "nope", "op": ">=", "value": 1}]), "unknown metric"),
                         (dict(rules=[{"metric": "revenue", "op": "~", "value": 1}]), "operator"),
                         (dict(rules=[{"metric": "revenue", "op": ">=", "value": "1; DROP"}]), "number"),
                         (dict(rules=[{"metric": "revenue", "op": ">=", "value": True}]), "number"),
                         (dict(rules=[{"metric": "revenue", "op": ">=", "value": 1}] * 13), "at most"),
                         (dict(rank_by="nope"), "unknown metric to rank by"), (dict(include_states=["blacklist"]), "include at least")):
            clean, err = self.v(**kw)
            self.assertIsNone(clean)
            self.assertIn(frag, err)


class TestScreenCash(unittest.TestCase):
    def test_cash_uses_newest_row_and_same_day_investments(self):
        def inst(rows):
            return {"units": {"USD": [{"end": e, "val": v, "form": f, "filed": e} for e, v, f in rows]}}
        facts = {"facts": {"us-gaap": {
            "CashAndCashEquivalentsAtCarryingValue": inst([("2025-12-31", 100.0, "10-K"), ("2026-09-30", 80.0, "10-Q")]),
            "ShortTermInvestments": inst([("2026-09-30", 50.0, "10-Q"), ("2025-12-31", 999.0, "10-K")])}}}
        self.assertEqual(scr.cash_position(facts), 130.0)
        self.assertIsNone(scr.cash_position({"facts": {"us-gaap": {}}}))

    def test_runway(self):
        self.assertEqual(scr.runway_months(120.0, 100.0, -50.0), 28.8)       # burn 50 a year, cash 120
        self.assertEqual(scr.runway_months(10.0, 100.0, 5.0), 999)           # funds itself
        self.assertIsNone(scr.runway_months(None, 100.0, -5.0))
        self.assertEqual(scr.SCREEN_VERSION, 7)


class _Req:
    def __init__(self, body): self._b = body
    async def json(self): return self._b


class TestEndpoints(unittest.TestCase):
    def setUp(self):
        store.init_db()
        ticker_state.ensure()
        with store._conn() as c:
            c.execute("DELETE FROM ticker_state")
        mk = lambda t, g: {"ticker": t, "cik": ("1" if t == "AAA" else "2"), "name": t, "exchange": "NYSE", "state": "active", "reason": "", "reason_kind": "screened",
                           "next_check": None, "s1": row(t, rev_growth_pct=g)["s1"], "scores": {"score": 4}}
        ticker_state.upsert_many([mk("AAA", 50.0), mk("BBB", 5.0)], "2026-11-12")
        self.p = mock.patch.object(appmod, "SLEEVES_FILE", os.path.join(tempfile.mkdtemp(dir=_TMP), "sleeves-test.json"))
        self.p.start()
        self.e = mock.patch.object(appmod, "_emit_config_changed")
        self.e.start()

    def tearDown(self):
        store.write_json(appmod.SLEEVES_FILE, {"custom": {}})      # app-state keys are by file name; leave nothing behind
        self.p.stop(); self.e.stop()

    def test_list_counts_matches_per_sleeve(self):
        out = appmod.get_sleeves()
        by = {s["id"]: s for s in out["sleeves"]}
        self.assertEqual(out["screened"], 2)
        self.assertEqual((by["growth"]["match_count"], by["growth"]["preview"][0]["ticker"]), (1, "AAA"))
        self.assertEqual(by["balance"]["match_count"], 0)
        self.assertTrue(by["balance"]["fill_gap"])
        self.assertTrue(any(m["key"] == "market_cap" and m["stage"] == "s2" for m in out["metrics"]))
        self.assertTrue(any(m["key"] == "insider_net_buying" and m["stage"] == "s3" for m in out["metrics"]))

    def test_custom_sleeve_create_preview_update_delete(self):
        body = {"name": "Slow growers", "rules": [{"metric": "rev_growth_pct", "op": "<=", "value": 10}]}
        pv = asyncio.run(appmod.post_sleeve_preview(_Req(body)))
        self.assertEqual((pv["match_count"], pv["considered"], pv["preview"][0]["ticker"]), (1, 2, "BBB"))
        made = asyncio.run(appmod.post_sleeve(_Req(body)))
        sid = made["id"]
        self.assertEqual(sid, "c-slow-growers")
        self.assertEqual(asyncio.run(appmod.post_sleeve(_Req(body)))["id"], "c-slow-growers-2")
        by = {s["id"]: s for s in appmod.get_sleeves()["sleeves"]}
        self.assertEqual(by[sid]["match_count"], 1)
        self.assertFalse(by[sid]["builtin"])
        upd = asyncio.run(appmod.post_sleeve(_Req({**body, "id": sid, "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 0}]})))
        self.assertTrue(upd["ok"])
        self.assertEqual(appmod.get_sleeve_matches(sid)["total"], 2)
        self.assertTrue(asyncio.run(appmod.delete_sleeve(sid, _Req({})))["ok"])
        self.assertNotIn(sid, {s["id"] for s in appmod.get_sleeves()["sleeves"]})

    def test_matches_page_and_search(self):
        # AAA 50% growth, BBB 5%: use a custom sleeve that accepts both
        body = {"name": "All", "rules": [{"metric": "rev_growth_pct", "op": ">=", "value": 0}]}
        sid = asyncio.run(appmod.post_sleeve(_Req(body)))["id"]
        full = appmod.get_sleeve_matches(sid, limit=50)
        self.assertEqual(full["total"], 2)
        first = appmod.get_sleeve_matches(sid, limit=1, offset=0)
        second = appmod.get_sleeve_matches(sid, limit=1, offset=1)
        self.assertEqual((len(first["rows"]), len(second["rows"]), first["total"]), (1, 1, 2))
        self.assertNotEqual(first["rows"][0]["ticker"], second["rows"][0]["ticker"])
        found = appmod.get_sleeve_matches(sid, q="bb")
        self.assertEqual((found["total"], found["rows"][0]["ticker"]), (1, "BBB"))
        self.assertEqual(appmod.get_sleeve_matches(sid, q="zzz")["total"], 0)

    def test_builtins_are_read_only_and_bad_requests_are_400(self):
        r = asyncio.run(appmod.post_sleeve(_Req({**sleeves.BUILTIN["growth"], "id": "growth"})))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(asyncio.run(appmod.delete_sleeve("growth", _Req({}))).status_code, 400)
        self.assertEqual(asyncio.run(appmod.delete_sleeve("nope", _Req({}))).status_code, 404)
        self.assertEqual(asyncio.run(appmod.post_sleeve(_Req({"name": "x", "rules": [{"metric": "zzz", "op": ">=", "value": 1}]}))).status_code, 400)
        self.assertEqual(appmod.get_sleeve_matches("nope").status_code, 404)


class TestCoworkFindings(unittest.TestCase):
    """Cowork's SLEEVES test, 2026-11-13."""
    def test_empty_rule_list_is_refused(self):
        clean, err = sleeves.validate({"name": "Empty", "rules": []})
        self.assertIsNone(clean)
        self.assertIn("at least one rule", err)

    def test_growth_dilution_cap_is_ignored_in_the_first_two_years_after_listing(self):
        young = row("FLY", share_change_pct=439.0, first_filed="2025-09-22")
        old = row("DIL", share_change_pct=439.0, first_filed="2015-01-01")
        ev = sleeves.evaluate(sleeves.BUILTIN["growth"], [young, old], TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["FLY"])
        self.assertEqual([r["value"] for r in sleeves.BUILTIN["growth"]["rules"] if r["metric"] == "share_change_pct"], [300])
        ons = sleeves.evaluate(sleeves.BUILTIN["growth"], [row("ONDS", share_change_pct=217.0)], TODAY)
        self.assertEqual(len(ons["matches"]), 1)

    def test_share_classes_of_one_company_collapse_to_one_row_with_an_also_note(self):
        a = {**row("SENEA", rev_growth_pct=40.0), "cik": "0000088205"}
        b = {**row("SENEB", rev_growth_pct=40.0), "cik": "0000088205"}
        other = {**row("OTHER", rev_growth_pct=35.0), "cik": "0000000001"}
        ev = sleeves.evaluate(sleeves.BUILTIN["growth"], [a, b, other], TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["SENEA", "OTHER"])
        self.assertEqual(ev["matches"][0]["also"], ["SENEB"])
        self.assertNotIn("also", a)                                # the input rows are not mutated

    def test_no_internal_note_leaks_into_the_built_in_text(self):
        for sl in sleeves.BUILTIN.values():
            self.assertNotIn("Proposed", sl["notes"])

    def test_form_blocks_empty_rules_inline_instead_of_saving_silently(self):
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ("Every rule needs a number in its value box.", "Add at least one rule", "SLEEVE_ERR", "also '+esc(r.also.join"):
            self.assertIn(needle, html, needle)
        self.assertNotIn("filter(r=>r.value!=='')", html)


class TestBrowseAndFloor(unittest.TestCase):
    """Cowork step 1 (2026-11-13): SHOW MORE stopped at 50, Growth ranking was topped by micro-revenue noise, narrow screens."""
    def test_growth_needs_real_revenue_so_percentages_on_tiny_sales_do_not_top_the_list(self):
        rows = [row("TINY", revenue=50_000.0, rev_growth_pct=230000.0), row("REAL", revenue=2e7, rev_growth_pct=60.0)]
        ev = sleeves.evaluate(sleeves.BUILTIN["growth"], rows, TODAY)
        self.assertEqual([r["ticker"] for r in ev["matches"]], ["REAL"])

    def test_rule_text_shows_money_in_plain_units(self):
        self.assertEqual(sleeves.describe_rule({"metric": "revenue", "op": ">=", "value": 10_000_000}),
                         "Revenue (last 12 months) >= $10M")
        self.assertEqual(sleeves.describe_rule({"metric": "cash", "op": ">", "value": 2.5e9}), "Cash and short-term investments > $2.5B")
        self.assertEqual(sleeves.describe_rule({"metric": "rev_growth_pct", "op": ">=", "value": 25}),
                         "Revenue growth, year over year >= 25%")

    def test_page_pages_searches_and_scrolls_tables_sideways(self):
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        for needle in ("function sleeveBrowser(sl)", "SHOW 50 MORE", "'Showing '+rows.length+' of '+total", "find a ticker or company",
                       "overflow-x:auto;max-width:100%;", "&offset='+off+(q?'&q='"):
            self.assertIn(needle, html, needle)


class TestPageWiring(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8") as f:
            self.html = f.read()

    def test_research_subtab_and_calls(self):
        for needle in ("{key:'sleeves',label:'SLEEVES',build:()=>sleevesContent()}", "fetch('/api/sleeves')", "'/api/sleeves/preview'",
                       "'/api/sleeves/'+encodeURIComponent(sl.id)+'/matches?limit=50&offset='", "function sleeveForm()", "+ NEW SLEEVE"):
            self.assertIn(needle, self.html, needle)

    def test_form_offers_the_fixed_menu_and_says_what_waits(self):
        for needle in ("all must pass", "never turned into rules", "would screen <b>", "COPY AS MY OWN"):
            self.assertIn(needle, self.html, needle)

    def test_loading_guard_and_state_vars(self):
        self.assertIn("if(SLEEVES_LOADING) return;", self.html)
        self.assertIn("SLEEVES=null, SLEEVES_LOADING=false", self.html)


if __name__ == "__main__":
    unittest.main()
