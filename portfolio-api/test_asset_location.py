"""RV-T4 / TASK-RV-036a: asset location. Hand-computed cases for the pure module, the API view, the chat tool and the wording rule."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="aloc-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import asset_location as al  # noqa: E402

BOND = {"equity": 0, "bond": 100, "cash": 0, "other": 0}
STOCKS = {"equity": 100, "bond": 0, "cash": 0, "other": 0}
LTCG15 = lambda ordinary, gain: round(gain * 0.15, 2)


def ln(t, b, v, y=None, typ="ETF", mix=None, gain=None, stock=False, industry=None):
    return {"ticker": t, "name": t, "bucket": b, "value": v, "gain": gain, "yield_pct": y, "type": typ, "mix": mix, "is_stock": stock, "industry": industry}


def run(lines, totals, ord_rate=0.24, qual_rate=0.15):
    return al.build(lines, totals, ord_rate, qual_rate, LTCG15, 80000.0)


class Character(unittest.TestCase):
    def test_what_is_known_and_what_is_not(self):
        c = al.character
        self.assertEqual(c({"type": "Cash"})["kind"], "ordinary")
        self.assertEqual(c({"type": "ETF", "mix": BOND})["ordinary_share"], 1.0)
        self.assertEqual(c({"type": "ETF", "mix": STOCKS})["kind"], "qualified")
        self.assertEqual(c({"type": "ETF", "mix": {"equity": 60, "bond": 40}})["kind"], "mixed")
        self.assertEqual(c({"type": "Stock", "is_stock": True, "industry": "Equity Real Estate Investment Trusts (REITs)"})["kind"], "ordinary")
        self.assertEqual(c({"type": "Stock", "is_stock": True, "industry": "Software"})["kind"], "qualified")
        self.assertEqual(c({"type": "Other"})["kind"], "unknown")                       # never guessed
        self.assertEqual(c({"type": "Mutual Fund"})["kind"], "unknown")                  # a fund with no class mix on file


class DragAndGrid(unittest.TestCase):
    def test_yearly_drag_is_dollar_yield_times_the_stated_rate(self):
        r = run([ln("BND", "taxable", 100000, 4.0, mix=BOND)], {"taxable": 100000})
        self.assertEqual(r["drag"]["yearly_current"], 960.0)                            # 100,000 x 4% x 24%
        r2 = run([ln("VTI", "taxable", 50000, 2.0, mix=STOCKS)], {"taxable": 50000})
        self.assertEqual(r2["drag"]["yearly_current"], 150.0)                            # 50,000 x 2% x 15%

    def test_nothing_is_taxed_in_a_sheltered_account(self):
        r = run([ln("BND", "pretax", 100000, 4.0, mix=BOND), ln("VTI", "roth", 50000, 2.0, mix=STOCKS)], {"pretax": 100000, "roth": 50000})
        self.assertEqual(r["drag"]["yearly_current"], 0.0)

    def test_column_totals_equal_the_bridge_totals_with_balance_only_accounts_shown_separately(self):
        r = run([ln("BND", "taxable", 100000, 4.0, mix=BOND)], {"taxable": 130000, "pretax": 80000, "roth": 0, "hsa": 5000})
        self.assertEqual(r["column_totals"], {"taxable": 130000.0, "pretax": 80000.0, "roth": 0.0, "hsa": 5000.0})
        self.assertEqual(r["other_balances_not_itemised"]["taxable"], 30000.0)

    def test_one_row_per_security_across_accounts_with_values_per_bucket(self):
        r = run([ln("VTI", "pretax", 150000, 1.4, mix=STOCKS), ln("VTI", "taxable", 60000, 1.4, mix=STOCKS)], {"pretax": 150000, "taxable": 60000})
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual((r["rows"][0]["values"]["pretax"], r["rows"][0]["values"]["taxable"], r["rows"][0]["total"]), (150000.0, 60000.0, 210000.0))


class Comparison(unittest.TestCase):
    LINES = [ln("BND", "taxable", 100000, 4.0, mix=BOND, gain=2000), ln("AAA", "pretax", 100000, 0.0, typ="Stock", stock=True),
             ln("DIV", "roth", 50000, 2.0, typ="Stock", stock=True), ln("HSAF", "hsa", 20000, 1.0, mix=STOCKS)]
    TOT = {"taxable": 100000, "pretax": 100000, "roth": 50000, "hsa": 20000}

    def setUp(self):
        self.r = run(self.LINES, self.TOT)
        self.c = self.r["comparison"]
        self.rows = {x["ticker"]: x["values"] for x in self.c["rows"]}

    def test_account_sizes_never_change_and_each_security_keeps_its_total(self):
        self.assertTrue(self.c["column_totals_unchanged"])
        for k in al.BUCKETS:
            self.assertAlmostEqual(sum(v[k] for v in self.rows.values()), {"taxable": 100000, "pretax": 100000, "roth": 50000, "hsa": 20000}[k], delta=1)
        for t, tot in (("BND", 100000), ("AAA", 100000), ("DIV", 50000), ("HSAF", 20000)):
            self.assertAlmostEqual(sum(self.rows[t].values()), tot, delta=1)

    def test_the_rules_are_applied(self):
        self.assertEqual(self.rows["BND"]["pretax"], 100000)                             # ordinary income goes to pre-tax first
        self.assertEqual(self.rows["DIV"]["roth"], 50000)                                # equal growth class: the higher yield takes the Roth room
        self.assertEqual(self.rows["AAA"]["taxable"], 100000)                            # zero-yield equity sits in taxable
        self.assertEqual(self.rows["HSAF"], {"taxable": 0.0, "pretax": 0.0, "roth": 0.0, "hsa": 20000.0})   # HSA untouched

    def test_yearly_difference_and_the_cost_of_moving(self):
        self.assertEqual(self.c["yearly_drag_now"], 960.0)
        self.assertEqual(self.c["yearly_drag_after"], 0.0)
        mv = {m["ticker"]: m for m in self.c["moves"]}
        self.assertEqual(mv["BND"]["taxable_sold"], 100000.0)
        self.assertEqual(mv["BND"]["unrealized_gain_on_sale"], 2000.0)
        self.assertEqual(self.c["sale"]["tax_if_sold"], 300.0)                            # 2,000 x 15% from the stub rate
        self.assertEqual(self.c["sale"]["years_of_difference_equal_to_sale_tax"], 0.3)    # 300 / 960

    def test_unknown_holdings_stay_where_they_are(self):
        r = run(self.LINES + [ln("MYST", "taxable", 5000, None, typ="Other")], {**self.TOT, "taxable": 105000})
        row = {x["ticker"]: x["values"] for x in r["comparison"]["rows"]}["MYST"]
        self.assertEqual(row["taxable"], 5000.0)

    def test_comparison_is_withheld_when_too_much_taxable_value_is_unknown(self):
        r = run([ln("BND", "taxable", 60000, 4.0, mix=BOND), ln("MYST", "taxable", 40000, None, typ="Other")], {"taxable": 100000})
        self.assertEqual(r["comparison"]["status"], "withheld")
        self.assertIn("40%", r["comparison"]["reason"])
        self.assertIn("rows", r)                                                          # the grid and the drag are still shown
        edge = run([ln("BND", "taxable", 75000, 4.0, mix=BOND), ln("MYST", "taxable", 25000, None, typ="Other")], {"taxable": 100000})
        self.assertEqual(edge["comparison"]["status"], "ready")                           # exactly 25% is allowed

    def test_a_sale_at_a_loss_is_flagged_for_the_wash_sale_rule_and_unknown_basis_gives_no_tax(self):
        r = run([ln("O", "taxable", 30000, 5.6, typ="Stock", stock=True, industry="REIT", gain=-2000), ln("VTI", "roth", 30000, 1.0, mix=STOCKS),
                 ln("BND0", "pretax", 30000, 0.0, mix=STOCKS)], {"taxable": 30000, "roth": 30000, "pretax": 30000})
        self.assertTrue(any("wash-sale" in c.lower() and "O" in c for c in r["cautions"]))
        r2 = run([ln("BND", "taxable", 100000, 4.0, mix=BOND, gain=None), ln("AAA", "pretax", 100000, 0.0, typ="Stock", stock=True)], {"taxable": 100000, "pretax": 100000})
        self.assertIsNone(r2["comparison"]["sale"]["tax_if_sold"])
        self.assertTrue(r2["comparison"]["sale"]["gain_unknown_for_some"])


class YieldKnowledge(unittest.TestCase):
    """Jason's real book: non-dividend growth stocks had NO yield on file (a feed returns nothing for a non-payer and the code turned a zero into
    'unknown'), so 100% of taxable value was unknown and the comparison was withheld. A non-payer is a known zero."""
    def setUp(self):
        if app is None:
            self.skipTest("app not importable")

    def test_yield_sources_in_order_and_unknown_stays_unknown(self):
        y = app._al_yield
        self.assertEqual(y("A", "Stock", {"valuation": {"dividend_yield_pct": 2.5}})[0], 2.5)
        self.assertEqual(y("A", "Stock", {"valuation": {"dividend_yield_pct": 0.0}})[0], 0.0)               # a feed zero is kept
        v, basis = y("N", "Stock", {"fundamentals": {"metrics": {"dividends_paid": None}}})
        self.assertEqual(v, 0.0)
        self.assertIn("no dividends paid", basis)
        v, basis = y("P", "Stock", {"valuation": {"market_cap_musd": 1000.0}, "fundamentals": {"metrics": {"dividends_paid": 2_000_000.0}}})
        self.assertEqual(v, 0.2)                                                                              # 2M / 1,000M
        self.assertIn("estimate", basis)
        self.assertEqual(y("P", "Stock", {"fundamentals": {"metrics": {"dividends_paid": 2_000_000.0}}}), (None, None))   # a payer with no market value: unknown
        self.assertEqual(y("O", "Stock", {"fundamentals": {"metrics": {"revenue": 5}}}), (None, None))       # an older cached read without the field
        self.assertEqual(y("F", "Mutual Fund", {"fundamentals": {"metrics": {"dividends_paid": None}}}), (None, None))   # a fund's yield is never inferred from this
        self.assertEqual(y("X", "Stock", {}), (None, None))

    def test_a_feed_zero_survives_the_valuation_read(self):
        data = {"metric": {"dividendYieldIndicatedAnnual": 0, "currentDividendYieldTTM": None, "peTTM": 25.0}}
        with mock.patch.object(app, "_finnhub_json", return_value=(data, True)):
            v = app._valuation("ZZYIELD0")
        self.assertEqual(v["dividend_yield_pct"], 0)
        data2 = {"metric": {"currentDividendYieldTTM": 1.7, "peTTM": 25.0}}
        with mock.patch.object(app, "_finnhub_json", return_value=(data2, True)):
            self.assertEqual(app._valuation("ZZYIELD1")["dividend_yield_pct"], 1.7)

    def test_sec_read_exposes_dividends_paid(self):
        import edgar
        fy = lambda yr, v: {"start": f"{yr - 1}-12-31", "end": f"{yr}-12-31", "val": v, "form": "10-K", "fp": "FY", "filed": f"{yr + 1}-02-20"}
        f = lambda **t: {"facts": {"us-gaap": {k: {"units": {"USD": rows}} for k, rows in t.items()}}}
        payer = edgar._metrics_from_facts("X", "1", f(Revenues=[fy(2024, 100), fy(2025, 120)], PaymentsOfDividendsCommonStock=[fy(2025, 4), fy(2024, 3)], Assets=[{"end": "2025-12-31", "val": 9, "form": "10-K", "fp": "FY", "filed": "2026-02-20"}]))
        self.assertEqual(payer["metrics"]["dividends_paid"], 4)
        none = edgar._metrics_from_facts("Y", "1", f(Revenues=[fy(2024, 100), fy(2025, 120)], Assets=[{"end": "2025-12-31", "val": 9, "form": "10-K", "fp": "FY", "filed": "2026-02-20"}]))
        self.assertIn("dividends_paid", none["metrics"])
        self.assertIsNone(none["metrics"]["dividends_paid"])

    def test_the_real_book_case_comparison_is_ready_not_withheld(self):
        d = {"accounts": [{"name": "Brokerage", "type": "Investments", "subtype": "Brokerage"}, {"name": "IRA", "type": "Investments", "subtype": "Traditional IRA"}],
             "by_account": [{"account": "Brokerage", "holdings": [{"ticker": "GROW1", "name": "g1", "value": 60000.0, "gain": 30000.0}, {"ticker": "GROW2", "name": "g2", "value": 40000.0, "gain": 10000.0}]},
                            {"account": "IRA", "holdings": [{"ticker": "BNDX", "name": "bond", "value": 50000.0, "gain": 0.0}]}],
             "consolidated": [{"ticker": "GROW1", "type": "Stock"}, {"ticker": "GROW2", "type": "Stock"}, {"ticker": "BNDX", "type": "ETF"}]}
        nopay = {"fundamentals": {"metrics": {"dividends_paid": None}}, "valuation": {"dividend_yield_pct": None}}
        briefs = {"briefs": {"GROW1": nopay, "GROW2": nopay, "BNDX": {"valuation": {"dividend_yield_pct": 4.0}}}}
        with mock.patch.object(app, "_data", return_value=d), mock.patch.object(app, "_read_retire", return_value={"annual_expenses_override": 120000, "filing_status": "mfj"}), \
             mock.patch.object(app, "_read_analysis", return_value=briefs), mock.patch.object(app, "_read_exp", return_value={}), \
             mock.patch.object(app, "_fund_mix", side_effect=lambda t: {"mix": BOND} if t == "BNDX" else None), \
             mock.patch.object(app, "_balances_by_tax_status", return_value={"taxable": 100000.0, "deferred": 50000.0, "roth": 0.0, "hsa": 0.0}):
            v = app.get_asset_location()
        self.assertEqual(v["drag"]["unknown_share"], 0.0)
        self.assertEqual(v["comparison"]["status"], "ready")
        row = next(r for r in v["rows"] if r["ticker"] == "GROW1")
        self.assertEqual((row["yield_pct"], row["yearly_drag"]), (0.0, 0.0))
        self.assertIn("no dividends paid", row["yield_basis"])


class RothTiesAndNotes(unittest.TestCase):
    def _rows(self, roth_a, roth_b):
        mk = lambda t, tax, roth: {"ticker": t, "name": t, "value": tax + roth, "gain": 0.0, "yield_pct": 0.0, "yield_basis": "none",
                                   "type": "Stock", "mix": None, "industry": None, "is_stock": True, "bucket": "taxable"}
        return mk

    def test_equal_class_and_yield_tie_keeps_what_is_already_in_roth_and_says_so(self):
        h = lambda t, b, v: {"ticker": t, "name": t, "bucket": b, "value": v, "gain": 0.0, "yield_pct": 0.0, "yield_basis": "x",
                             "type": "Stock", "mix": None, "industry": None, "is_stock": True}
        lines = [h("AAA", "taxable", 50000.0), h("ZZZ", "roth", 50000.0), h("MMM", "taxable", 50000.0)]
        res = al.build(lines, {"taxable": 100000.0, "pretax": 0.0, "roth": 50000.0, "hsa": 0.0}, 0.24, 0.15)
        cmp_ = res["comparison"]
        rows = {r["ticker"]: r["values"] for r in cmp_["rows"]}
        self.assertEqual(rows["ZZZ"]["roth"], 50000)                  # already in Roth, equal rank: not displaced by ticker order
        self.assertTrue(cmp_["all_known_yields_zero"])
        self.assertEqual(sorted(cmp_["growth_ties"]), ["AAA", "MMM", "ZZZ"])
        self.assertIn("not a forecast", cmp_["growth_measure"])

    def test_chat_guidance_lists_every_required_part(self):
        src = open(os.path.join(os.path.dirname(__file__), "app.py"), encoding="utf-8").read()
        for need in ("ALWAYS include every one of these", "all_known_yields_zero", "growth_measure", "PLAN > ASSET LOCATION"):
            self.assertIn(need, src)

    def test_page_shows_the_zero_yield_note_and_growth_measure(self):
        html = open(os.path.join(os.path.dirname(__file__), "static", "index.html"), encoding="utf-8").read()
        self.assertIn("no taxable income to move", html)
        self.assertIn("c.growth_measure", html)


class ChatFacts(unittest.TestCase):
    def _view(self):
        h = lambda t, b, v: {"ticker": t, "name": t, "bucket": b, "value": v, "gain": 0.0, "yield_pct": 0.0, "yield_basis": "x",
                             "type": "Stock", "mix": None, "industry": None, "is_stock": True}
        lines = [h("AAA", "taxable", 50000.0), h("ZZZ", "roth", 50000.0), h("MMM", "taxable", 50000.0)]
        return al.build(lines, {"taxable": 100000.0, "pretax": 0.0, "roth": 50000.0, "hsa": 0.0}, 0.24, 0.15)

    def test_facts_are_app_written_and_complete(self):
        t = al.facts_text(self._view(), "where should I hold my bond fund?")
        for need in ("**AAA**", "**ZZZ**", "The rules the comparison uses", "difference $0", "no taxable income to move", "Growth measure", "Unknown:", "PLAN > ASSET LOCATION"):
            self.assertIn(need, t)

    def test_named_holdings_come_first_alone(self):
        t = al.facts_text(self._view(), "why does ZZZ get less Roth than AAA?")
        self.assertIn("**ZZZ**", t)
        self.assertIn("**AAA**", t)
        self.assertNotIn("**MMM**", t)
        self.assertIn("Roth under the rule", t)

    def test_commentary_rule_checks_the_premise_and_bans_invented_reasons(self):
        r = al.COMMENTARY_RULE
        self.assertIn("premise", r)
        self.assertIn("MAGI", r)
        self.assertNotRegex(r.lower(), r"you should")

    def test_unusable_view_gives_nothing(self):
        self.assertEqual(al.facts_text({}, "x"), "")
        self.assertEqual(al.facts_text({"rows": []}, "x"), "")

    def test_chat_loop_prints_facts_first_then_labelled_commentary(self):
        src = open(os.path.join(os.path.dirname(__file__), "app.py"), encoding="utf-8").read()
        for need in ("asset_location.facts_text(result, user_msg)", "asset_location.COMMENTARY_RULE", "answer_check.compose(clean, 0, loc_facts"):
            self.assertIn(need, src)


class Wording(unittest.TestCase):
    def test_no_instruction_to_buy_or_sell_anywhere(self):
        r = run(Comparison.LINES, Comparison.TOT)
        text = " ".join(al.RULES + r["cautions"] + [r["comparison"]["label"]])
        self.assertNotRegex(text, r"(?i)\byou should\b|\bshould (?:sell|buy)\b|\bwe recommend\b|\bbuy more\b|\bsell now\b")
        self.assertIn("for study", r["comparison"]["label"])
        html = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
        block = html[html.index("function alocContent()"):html.index("// ---- HOUSEHOLD REPORT (RV-T5)")]
        self.assertNotRegex(block, r"(?i)\bbuy\b|\byou should\b|\brecommend")


try:
    import app
except Exception:
    app = None


@unittest.skipIf(app is None, "app not importable")
class ApiView(unittest.TestCase):
    def test_view_uses_the_bridge_totals_the_tax_panel_rates_and_notices(self):
        d = {"accounts": [{"name": "Brokerage", "type": "Investments", "subtype": "Brokerage"}, {"name": "IRA", "type": "Investments", "subtype": "Traditional IRA"}],
             "by_account": [{"account": "Brokerage", "holdings": [{"ticker": "BND", "name": "Bond", "value": 100000.0, "gain": 2000.0}]},
                            {"account": "IRA", "holdings": [{"ticker": "VTI", "name": "Stock mkt", "value": 100000.0, "gain": 0.0}]}],
             "consolidated": [{"ticker": "BND", "type": "ETF"}, {"ticker": "VTI", "type": "ETF"}]}
        briefs = {"briefs": {"BND": {"valuation": {"dividend_yield_pct": 4.0}}, "VTI": {"valuation": {"dividend_yield_pct": 1.4}}}}
        with mock.patch.object(app, "_data", return_value=d), mock.patch.object(app, "_read_retire", return_value={"annual_expenses_override": 120000, "filing_status": "mfj"}), \
             mock.patch.object(app, "_read_analysis", return_value=briefs), mock.patch.object(app, "_read_exp", return_value={}), \
             mock.patch.object(app, "_fund_mix", side_effect=lambda t: {"mix": BOND if t == "BND" else STOCKS}), \
             mock.patch.object(app, "_balances_by_tax_status", return_value={"taxable": 100000.0, "deferred": 100000.0, "roth": 0.0, "hsa": 0.0}):
            v = app.get_asset_location()
        self.assertEqual(v["column_totals"], {"taxable": 100000.0, "pretax": 100000.0, "roth": 0.0, "hsa": 0.0})
        self.assertEqual(v["rates"]["ordinary"], app.tax.marginal_ordinary_rate(max(0, 120000 - app.tax.standard_deduction("mfj", None, None)), "mfj"))
        row = next(x for x in v["rows"] if x["ticker"] == "BND")
        self.assertEqual(row["yearly_drag"], round(100000 * 0.04 * v["rates"]["ordinary"], 2))     # dollar yield x the marginal rate shown
        self.assertEqual(v["notice"], app.notices.ESTIMATE)
        self.assertIn("PLAN > ASSET LOCATION", v["how_to_answer"])                                              # RETEST-043: the chat answer must point to the page
        self.assertIn("withheld", v["how_to_answer"])
        self.assertNotRegex(v["how_to_answer"], r"(?i)\byou should\b|\bwe recommend\b")
        self.assertEqual(v["comparison"]["status"], "ready")
        self.assertTrue(v["comparison"]["column_totals_unchanged"])

    def test_chat_tool_is_registered_read_only_and_dispatched(self):
        self.assertIn("get_asset_location", [t["function"]["name"] for t in app.FIDUCIARY_TOOLS])
        self.assertNotIn("get_asset_location", [t["function"]["name"] for t in app.STRATEGIST_TOOLS])
        self.assertIn("get_asset_location", app.FIDUCIARY_PROMPT)
        desc = next(t["function"]["description"] for t in app.FIDUCIARY_TOOLS if t["function"]["name"] == "get_asset_location")
        self.assertIn("where should I hold my bond fund", desc)
        with mock.patch.object(app, "_asset_location_view", return_value={"ok": 1}):
            self.assertEqual(app._fiduciary_dispatch("get_asset_location", {}), {"ok": 1})

    def test_the_page_lists_the_tab_with_the_estimate_notice(self):
        html = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
        self.assertIn("{key:'location',label:'ASSET LOCATION',build:()=>alocContent()}", html)
        self.assertRegex(html, r"NOTICE_ESTIMATE_KEYS=new Set\(\[[^\]]*'location'")


if __name__ == "__main__":
    unittest.main()
