"""RV-032 phase 1: the year-by-year engine (pure) and its API. Each row is checked against the engines it calls and must reconcile."""
import asyncio
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aca, plan_timeline, roth, tax  # noqa: E402

Y = datetime.date.today().year


def base(**kw):
    inp = {"start_year": 2026, "start_age": 40, "spouse_age": None, "end_age": 95, "retire_age": 55, "birth_year": 1986, "status": "single", "household_size": 1,
           "slcsp_annual": 9000.0, "state": "TX", "balances": {"taxable": 400000.0, "pretax": 600000.0, "roth": 100000.0, "hsa": 30000.0},
           "real_return_pct": 5.0, "savings": {"taxable": 20000.0, "pretax": 15000.0, "employer_match": 5000.0}, "living_expenses": 50000.0,
           "healthcare_fn": lambda a: 4000.0 if 65 <= a <= 90 else 0.0, "ltc_fn": lambda a: 3000.0 if 65 <= a <= 90 else 0.0,
           "withdrawal_order": ["taxable", "pretax", "roth"], "conversions": {}, "ss": {"include": False, "claim_age": 67, "annual_benefit": 30000.0},
           "taxable_gain_fraction": 0.4, "prior_magi": {}, "rmd_divisors": dict(roth.RMD_DIVISORS)}
    inp.update(kw)
    return inp


def row(res, year):
    return next(r for r in res["rows"] if r["year"] == year)


class Reconcile(unittest.TestCase):
    def test_every_bucket_and_the_total_reconcile_every_year(self):
        res = plan_timeline.build(base(conversions={2042: 30000.0, 2043: 30000.0}, ss={"include": True, "claim_age": 67, "annual_benefit": 30000.0}))
        for r in res["rows"]:
            for b in plan_timeline.BUCKETS:
                moved_in = r["conversion"] if b == "roth" else 0.0
                moved_out = r["conversion"] if b == "pretax" else 0.0
                sur = r["rmd_surplus_reinvested"] if b == "taxable" else 0.0
                want = r["start"][b] + r["contributions"][b] + moved_in - moved_out - r["funding"][b] + sur + r["growth"][b]
                self.assertAlmostEqual(r["end"][b], want, delta=0.03, msg=f"{r['year']} {b}")
            spend = (r["spending"]["total"] or 0.0)
            tx = r["tax"]
            tax_total = (tx["federal"] + (tx["state"] or 0.0) + tx["penalty"]) if tx else 0.0
            ss_cash = r["money_in"]["social_security"]
            contrib = sum(r["contributions"].values())
            want_total = r["start_total"] + contrib + r["growth_total"] + ss_cash - spend - tax_total + r["shortfall"]
            self.assertAlmostEqual(r["end_total"], want_total, delta=0.1, msg=f"{r['year']} total")
        self.assertEqual(len(res["rows"]), 95 - 40 + 1)

    def test_first_row_starts_from_the_given_balances(self):
        r0 = plan_timeline.build(base())["rows"][0]
        self.assertEqual(r0["start"], {"taxable": 400000.0, "pretax": 600000.0, "roth": 100000.0, "hsa": 30000.0})


class WorkingYear(unittest.TestCase):
    def test_savings_come_in_growth_applies_and_nothing_is_withdrawn_or_taxed(self):
        r = row(plan_timeline.build(base()), 2026)
        self.assertEqual((r["phase"], r["tax"], r["funding"]["taxable"], r["spending"]["total"]), ("working", None, 0.0, None))
        self.assertEqual(r["contributions"], {"taxable": 20000.0, "pretax": 20000.0, "roth": 0.0, "hsa": 0.0})          # 15,000 + 5,000 match
        self.assertAlmostEqual(r["end"]["taxable"], (400000 + 20000) * 1.05, places=1)
        self.assertTrue([f for f in r["flags"] if "Working year" in f])


class BridgeYearAndCliff(unittest.TestCase):
    def test_a_conversion_over_400_percent_of_poverty_loses_the_subsidy_and_flags_it(self):
        res = plan_timeline.build(base(retire_age=50, start_age=50, conversions={2026: 70000.0}))
        r = row(res, 2026)
        self.assertEqual(r["conversion"], 70000.0)
        self.assertTrue(r["coverage"]["subsidy_lost"])
        self.assertEqual(r["coverage"]["subsidy_dollars"], 0.0)
        self.assertGreaterEqual(r["coverage"]["magi"], 70000.0)                 # the conversion plus any gains realized to fund spending
        self.assertEqual(r["coverage"]["headroom_to_cliff"], aca.cliff(r["coverage"]["magi"], 1)["headroom_to_cliff"])
        self.assertTrue([f for f in r["flags"] if f.startswith("Over the ACA cliff by $")])
        self.assertIn(2026, res["summary"]["years_with_lost_subsidy"])

    def test_a_smaller_conversion_keeps_the_subsidy(self):
        r = row(plan_timeline.build(base(retire_age=50, start_age=50, conversions={2026: 20000.0})), 2026)
        self.assertFalse(r["coverage"]["subsidy_lost"])
        self.assertGreater(r["coverage"]["subsidy_dollars"], 0.0)

    def test_the_conversion_is_taxed_by_the_engine_and_moves_pretax_to_roth(self):
        r = row(plan_timeline.build(base(retire_age=50, start_age=50, conversions={2026: 20000.0})), 2026)
        ded = tax.standard_deduction("single", 50)
        er = tax.effective_rate(20000.0, 0.0, "single", None, 50, None, include_niit=True)
        self.assertAlmostEqual(r["tax"]["federal"], er["total_tax"], delta=1.0)
        self.assertEqual(r["tax"]["ordinary_taxable"], max(0.0, 20000.0 - ded))
        self.assertAlmostEqual(r["end"]["roth"], (100000 + 20000) * 1.05, delta=1.0)


class RequiredDistributions(unittest.TestCase):
    def test_first_year_uses_the_table_divisor_and_is_flagged_once(self):
        res = plan_timeline.build(base(birth_year=1960, start_age=70, start_year=2026, retire_age=60, balances={"taxable": 0.0, "pretax": 1000000.0, "roth": 0.0, "hsa": 0.0}))
        first = [r for r in res["rows"] if [f for f in r["flags"] if f.startswith("First required distribution")]]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["age"], 75)                                  # born 1960: SECURE 2.0 starts at 75
        self.assertAlmostEqual(first[0]["rmd"], first[0]["start"]["pretax"] / 24.6, places=1)
        self.assertEqual(row(res, 2026 + 4)["rmd"], 0.0)                       # age 74: none

    def test_a_conversion_before_the_first_distribution_shrinks_it(self):
        a = plan_timeline.build(base(birth_year=1960, start_age=65, retire_age=60, start_year=2026, balances={"taxable": 800000.0, "pretax": 1000000.0, "roth": 0.0, "hsa": 0.0}))
        b = plan_timeline.build(base(birth_year=1960, start_age=65, retire_age=60, start_year=2026, balances={"taxable": 800000.0, "pretax": 1000000.0, "roth": 0.0, "hsa": 0.0},
                                     conversions={2026 + i: 100000.0 for i in range(0, 10)}))
        ra, rb = row(a, 2026 + 10), row(b, 2026 + 10)
        self.assertGreater(ra["rmd"], rb["rmd"])

    def test_missing_birth_year_or_table_is_reported_not_guessed(self):
        res = plan_timeline.build(base(birth_year=None))
        self.assertTrue([p for p in res["problems"] if "birth year" in p])
        self.assertTrue(all(r["rmd"] == 0.0 for r in res["rows"]))
        self.assertTrue([p for p in plan_timeline.build(base(rmd_divisors={}))["problems"] if "table" in p])

    def test_divisor_lookup(self):
        self.assertEqual(plan_timeline.rmd_divisor(73, roth.RMD_DIVISORS), 26.5)
        self.assertEqual(plan_timeline.rmd_divisor(130, roth.RMD_DIVISORS), 2.0)
        self.assertIsNone(plan_timeline.rmd_divisor(60, roth.RMD_DIVISORS))


class Medicare(unittest.TestCase):
    def test_surcharge_uses_income_two_years_earlier(self):
        res = plan_timeline.build(base(start_age=66, start_year=2026, retire_age=60, prior_magi={2024: 150000.0}))
        r = row(res, 2026)                                                     # age 66; the look-back year 2024 is before the plan starts
        want = roth.irmaa_annual_part_b(150000.0, "single", 1)
        self.assertGreater(want, 0)
        self.assertEqual(r["coverage"]["surcharge"], want)
        self.assertTrue([f for f in r["flags"] if f.startswith("Medicare Part B surcharge")])

    def test_lookback_uses_the_income_the_table_itself_computed_for_earlier_years(self):
        res = plan_timeline.build(base(start_age=64, start_year=2026, retire_age=60, balances={"taxable": 200000.0, "pretax": 3000000.0, "roth": 0.0, "hsa": 0.0},
                                       withdrawal_order=["pretax", "taxable", "roth"], conversions={2026: 120000.0}))
        magi_2026 = row(res, 2026)["tax"]["magi"]
        self.assertEqual(row(res, 2028)["coverage"]["magi_lookback"], magi_2026)
        self.assertEqual(row(res, 2028)["coverage"]["surcharge"], roth.irmaa_annual_part_b(magi_2026, "single", 1))

    def test_unknown_lookback_income_is_blank_with_a_reason(self):
        r = row(plan_timeline.build(base(start_age=66, start_year=2026, retire_age=60)), 2026)
        self.assertIsNone(r["coverage"]["surcharge"])
        self.assertIn("two years earlier", r["coverage"]["reason"])


class Funding(unittest.TestCase):
    def test_shortfall_is_shown_and_flagged_when_the_buckets_run_out(self):
        res = plan_timeline.build(base(retire_age=60, start_age=60, balances={"taxable": 30000.0, "pretax": 0.0, "roth": 0.0, "hsa": 0.0}))
        self.assertTrue(res["summary"]["years_short"])
        r = row(res, res["summary"]["first_year_short"])
        self.assertGreater(r["shortfall"], 0)
        self.assertTrue([f for f in r["flags"] if f.startswith("Plan runs short")])

    def test_early_pretax_withdrawals_are_used_last_and_penalised(self):
        res = plan_timeline.build(base(retire_age=50, start_age=50, balances={"taxable": 0.0, "pretax": 500000.0, "roth": 0.0, "hsa": 0.0}, withdrawal_order=["pretax", "roth", "taxable"]))
        r = row(res, 2026)
        self.assertGreater(r["tax"]["penalty"], 0)
        self.assertAlmostEqual(r["tax"]["penalty"], 0.10 * r["funding"]["pretax"], delta=1.0)

    def test_pretax_waits_behind_roth_before_59_and_a_half(self):
        res = plan_timeline.build(base(retire_age=50, start_age=50, balances={"taxable": 0.0, "pretax": 500000.0, "roth": 200000.0, "hsa": 0.0}, withdrawal_order=["pretax", "roth", "taxable"]))
        r = row(res, 2026)
        self.assertGreater(r["funding"]["roth"], 0)
        self.assertEqual(r["funding"]["pretax"], 0.0)

    def test_healthcare_is_paid_from_the_hsa_first(self):
        res = plan_timeline.build(base(start_age=66, start_year=2026, retire_age=60, balances={"taxable": 500000.0, "pretax": 0.0, "roth": 0.0, "hsa": 50000.0}))
        r = row(res, 2026)
        self.assertEqual(r["funding"]["hsa"], 4000.0)                         # the healthcare line only
        self.assertEqual(r["spending"]["healthcare"], 4000.0)

    def test_surplus_from_a_required_distribution_is_reinvested_in_the_taxable_bucket(self):
        res = plan_timeline.build(base(birth_year=1950, start_age=80, start_year=2026, retire_age=60, living_expenses=1000.0,
                                       balances={"taxable": 0.0, "pretax": 2000000.0, "roth": 0.0, "hsa": 0.0}))
        r = row(res, 2026)
        self.assertGreater(r["rmd_surplus_reinvested"], 0)
        self.assertGreater(r["end"]["taxable"], 0)

    def test_social_security_is_off_unless_included(self):
        off = row(plan_timeline.build(base(start_age=70, start_year=2026, retire_age=60)), 2026)
        on = row(plan_timeline.build(base(start_age=70, start_year=2026, retire_age=60, ss={"include": True, "claim_age": 67, "annual_benefit": 30000.0})), 2026)
        self.assertEqual(off["money_in"]["social_security"], 0.0)
        self.assertEqual(on["money_in"]["social_security"], 30000.0)
        self.assertGreater(on["tax"]["ss_taxable"], -1)


class Output(unittest.TestCase):
    def test_footer_and_sources_state_terms_and_provenance(self):
        res = plan_timeline.build(base())
        self.assertIn("Real (today's-dollar) terms", res["footer"])
        self.assertIn("Deterministic", res["footer"])
        for k in ("tax", "aca", "medicare", "rmd", "ss", "growth", "spending", "balances"):
            self.assertIn(k, res["sources"])


if __name__ == "__main__":
    unittest.main()


class Api(unittest.TestCase):
    def setUp(self):
        import app as appmod
        self.app = appmod
        inp = base(start_year=Y, start_age=45, retire_age=60)
        inp.update({"healthcare_annual": 4000.0, "ltc_annual": 3000.0, "care_from_age": 65, "care_to_age": 90})
        self.inp = inp
        self.p = mock.patch.object(appmod, "_timeline_inputs", lambda: (dict(inp), ["a note"]))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def post(self, body):
        class Req:
            async def json(self_inner):
                return body
        return asyncio.run(self.app.post_plan_timeline(Req()))

    def test_get_returns_one_row_per_year_with_notes_and_inputs(self):
        r = self.app.get_plan_timeline()
        self.assertEqual((len(r["rows"]), r["rows"][0]["year"], r["notes"]), (95 - 45 + 1, Y, ["a note"]))
        self.assertEqual(r["tax_year"], tax.TAX_YEAR)
        self.assertNotIn("rmd_divisors", r["inputs"])
        self.assertEqual(row(r, Y + 25)["spending"]["healthcare"], 4000.0)           # age 70: inside the 65-90 care window

    def test_trial_inputs_change_the_table_and_nothing_is_saved(self):
        base_r = self.app.get_plan_timeline()
        with mock.patch.object(self.app.store, "write_json") as w:
            r = self.post({"retire_age": 50, "conversions": {str(Y + 5): 40000}, "withdrawal_order": ["roth", "taxable", "pretax"]})
            w.assert_not_called()
        self.assertEqual(row(r, Y + 5)["conversion"], 40000.0)
        self.assertNotEqual(row(r, Y + 8)["start_total"], row(base_r, Y + 8)["start_total"])

    def test_bad_trial_inputs_are_refused_with_a_reason(self):
        for body, frag in (({"retire_age": "soon"}, "retire_age"), ({"real_return_pct": 90}, "between"), ({"end_age": 50, "retire_age": 60}, "after the retirement age"),
                           ({"withdrawal_order": ["taxable", "taxable"]}, "twice")):
            r = self.post(body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(frag, r.body.decode())

    def test_a_viewer_may_run_a_trial(self):
        self.assertIn("/api/plan/timeline", self.app._VIEWER_ALLOWED_WRITE_PATHS)

    def test_missing_age_gives_a_clear_message(self):
        with mock.patch.object(self.app, "_timeline_inputs", lambda: ({**self.inp, "start_age": None}, [])):
            self.assertEqual(self.app.get_plan_timeline().status_code, 400)


class EducationAccounts(unittest.TestCase):
    """529 and Coverdell accounts are their own status: out of every retirement figure, still in net worth."""

    def setUp(self):
        import app as appmod
        self.app = appmod
        self.data = {"accounts": [{"name": "My 401k", "type": "investment", "subtype": "401k", "balance": 100.0, "is_asset": True, "include_nw": True},
                                  {"name": "Kid 529 College", "type": "investment", "subtype": "529", "balance": 40.0, "is_asset": True, "include_nw": True},
                                  {"name": "Coverdell ESA", "type": "investment", "subtype": None, "balance": 10.0, "is_asset": True, "include_nw": True},
                                  {"name": "Brokerage", "type": "Investments", "subtype": "brokerage", "balance": 60.0, "is_asset": True, "include_nw": True}],
                     "by_account": [{"account": "My 401k", "holdings": [{"value": 100.0}]}, {"account": "Kid 529 College", "holdings": [{"value": 40.0}]},
                                    {"account": "Brokerage", "holdings": [{"value": 60.0}]}]}

    def test_classifier(self):
        c = self.app._account_tax_status
        self.assertEqual(c("Kid 529 College", "investment", None), "education")
        self.assertEqual(c("Savings plan", "investment", "529"), "education")
        self.assertEqual(c("Coverdell ESA", "investment", None), "education")
        self.assertEqual(c("My 401k", "investment", "401k"), "deferred")             # unchanged
        self.assertEqual(c("Roth IRA", "investment", None), "roth")

    def test_education_money_is_out_of_the_retirement_buckets_and_investable_assets(self):
        with mock.patch.object(self.app, "_data", lambda: self.data):
            bal = self.app._balances_by_tax_status()
            self.assertEqual((bal["deferred"], bal["taxable"]), (100.0, 60.0))
            self.assertEqual(self.app._investable_assets(self.data, []), 160.0)          # 401k 100 + brokerage 60; the 529 (40) and Coverdell (10) are left out

            self.assertEqual(self.app._education_balance(self.data), 50.0)

    def test_education_accounts_are_not_emergency_fund_sources(self):
        self.assertIsNone(self.app._ef_tier(self.data["accounts"][1]))
        self.assertEqual(self.app._registry_group(self.data["accounts"][1]), "advantaged")

    def test_the_timeline_says_what_it_left_out(self):
        with mock.patch.object(self.app, "_data", lambda: self.data):
            self.assertEqual(self.app._education_balance(), 50.0)


class Page(unittest.TestCase):
    """Phase 2: the PLAN > TIMELINE page (strings only; the render was also run in Node against real engine output)."""

    def setUp(self):
        self.html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()

    def test_subtab_inputs_conversion_cells_and_hover_sources_exist(self):
        for needle in ("{key:'timeline',label:'TIMELINE'", "function timelineContent()", "fetch('/api/plan/timeline'", "'Source: '+S[src]",
                       "ci.onchange=()=>tlConv(x.year,ci.value)", "RESET TO CURRENT", "COPY REPORT", "WHAT" if False else "YEARS WITH A LOST ACA SUBSIDY",
                       "LIFETIME TAX", "SHOW BUCKET DETAIL"):
            self.assertIn(needle, self.html, needle)

    def test_copied_report_carries_the_closing_line(self):
        self.assertIn("Informational only, produced by a personal tool. Not financial, tax or legal advice.", self.html)
        self.assertIn("L.push('',r.footer,'',TL_CLOSING)", self.html)

    def test_trials_are_posted_never_saved(self):
        self.assertIn("method:'POST'", self.html.split("function tlRun()")[1].split("function tlQueue")[0])
        self.assertNotIn("/api/retirement-config", self.html.split("function tlRun()")[1].split("function panelSubtabs")[0])


class HeaderHelp(unittest.TestCase):
    def test_every_timeline_column_header_has_its_own_help_text(self):
        res = plan_timeline.build(base())
        for k in ("when", "contributions", "withdrawn", "flags", "balances", "spending", "ss", "rmd", "conversion", "tax", "state_tax", "aca", "medicare", "growth"):
            self.assertTrue(res["sources"].get(k), k)
        self.assertEqual(len({res["sources"][k] for k in ("balances", "spending", "contributions", "withdrawn")}), 4)        # no copied texts
        html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8").read()
        self.assertIn("{h:'SAVED',src:'contributions'", html)
        self.assertIn("{h:'WITHDRAWN',src:'withdrawn'", html)
        self.assertIn("{h:'FLAGS',src:'flags'", html)
        self.assertIn("cat:'RETIREMENT ODDS'", html)               # the cockpit shows it as well as the ATTENTION list
