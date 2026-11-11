"""Long-term care planning: dated costs, scenarios, insurance offset, probability-weighted yearly line, retirement integration, chat tool."""
import datetime, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="ltc-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import ltc  # noqa: E402
import app as appmod  # noqa: E402

TODAY = datetime.date(2026, 11, 10)
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()


class TestTable(unittest.TestCase):
    def test_dated_constant_with_source(self):
        t = ltc.table(TODAY)
        v = t["values"]
        self.assertEqual((v["home_health_aide"], v["assisted_living"], v["nursing_home_semi_private"], v["nursing_home_private"]),
                         (77792.0, 70800.0, 111325.0, 127750.0))
        self.assertEqual((v["p_paid_care"], v["p_severe_needs"]), (0.48, 0.70))
        self.assertEqual(t["year"], 2026)
        self.assertIn("genworth", t["source_url"])

    def test_registry_lists_it(self):
        import grounding
        r = [x for x in grounding.registry() if x["key"] == "ltc.cost_of_care"]
        self.assertEqual(len(r), 1)
        self.assertNotIn("error", r[0])


class TestCompute(unittest.TestCase):
    def test_typical_path_costs_in_todays_dollars_with_no_growth(self):
        r = ltc.compute(82, growth_pct=0.0, today=TODAY)          # already at the start age: no lead years
        self.assertEqual([y["service"] for y in r["years"]], ["home health aide", "assisted living", "nursing home (semi-private)"])
        self.assertEqual(r["total_cost"], round(77792 + 70800 + 111325, 2))
        self.assertEqual((r["insurance_pays"], r["uncovered"]), (0.0, r["total_cost"]))

    def test_real_cost_growth_to_the_start_age(self):
        r = ltc.compute(52, growth_pct=1.0, start_age=82, today=TODAY)   # 30 years of lead
        self.assertEqual(r["lead_years"], 30)
        self.assertAlmostEqual(r["years"][0]["cost"], 77792 * 1.01 ** 30, delta=1)
        self.assertGreater(r["years"][2]["cost"], r["years"][2]["base"] * 1.01 ** 30 - 1)

    def test_unknown_age_applies_no_growth(self):
        r = ltc.compute(None, growth_pct=3.0, today=TODAY)
        self.assertEqual(r["years"][0]["cost"], 77792.0)

    def test_scenarios(self):
        base = {s: ltc.compute(82, scenario=s, growth_pct=0.0, today=TODAY) for s in ltc.SCENARIOS}
        self.assertEqual(base["home_care"]["total_cost"], round(3 * 77792, 2))
        self.assertEqual(base["assisted_living"]["total_cost"], round(3 * 70800, 2))
        self.assertEqual(base["nursing_home"]["total_cost"], round(3 * 111325, 2))
        self.assertEqual(base["stress"]["total_cost"], round(5 * 127750, 2))
        self.assertEqual(len(base["stress"]["years"]), 5)
        self.assertEqual(ltc.compute(82, scenario="bogus", growth_pct=0.0, today=TODAY)["scenario"], "typical_path")

    def test_insurance_pays_per_year_up_to_the_daily_benefit_for_the_benefit_period(self):
        r = ltc.compute(82, scenario="assisted_living", growth_pct=0.0, insurance_daily=150, insurance_years=2, today=TODAY)
        # $150/day = $54,750/yr is below the $70,800 cost: the policy pays that much for 2 years, nothing in year 3
        self.assertEqual([y["insurance"] for y in r["years"]], [54750.0, 54750.0, 0.0])
        self.assertEqual(r["uncovered"], round(3 * 70800 - 2 * 54750, 2))
        r2 = ltc.compute(82, scenario="home_care", growth_pct=0.0, insurance_daily=1000, insurance_years=1.5, today=TODAY)
        self.assertEqual([y["insurance"] for y in r2["years"]], [77792.0, 38896.0, 0.0])   # capped at cost; half of year two

    def test_expected_cost_is_probability_times_uncovered_per_person(self):
        one = ltc.compute(82, growth_pct=0.0, today=TODAY)
        two = ltc.compute(82, people=2, growth_pct=0.0, today=TODAY)
        self.assertEqual(one["expected"]["household_expected_uncovered"], round(0.48 * one["uncovered"], 2))
        self.assertEqual(two["expected"]["household_expected_uncovered"], round(2 * 0.48 * two["uncovered"], 2))
        self.assertEqual(one["expected"]["annual"], round(one["expected"]["household_expected_uncovered"] / 25, 2))
        self.assertIsNone(one["both_need_care_note"])
        self.assertIn("twice", two["both_need_care_note"])

    def test_funding_view_uses_investable_and_hsa_surplus(self):
        r = ltc.compute(82, growth_pct=0.0, investable=1_000_000, hsa_surplus=50_000, today=TODAY)
        self.assertEqual(r["funding"]["uncovered_pct_of_investable"], round(r["uncovered"] / 10000, 1))
        self.assertEqual(r["funding"]["uncovered_after_hsa_surplus"], round(r["uncovered"] - 50000, 2))

    def test_no_table_is_a_reasoned_not_ok(self):
        with mock.patch.object(ltc, "table", return_value={"values": {}, "year": None, "verified": None, "source": None, "source_url": None, "stale": True}):
            r = ltc.compute(60)
        self.assertFalse(r["ok"])
        self.assertIn("no long-term care cost table", r["reason"])

    def test_it_explains_medicare_medicaid_and_hsa(self):
        r = ltc.compute(82, growth_pct=0.0, today=TODAY)
        self.assertIn("Medicare does not pay for custodial care", r["medicare_note"])
        self.assertIn("5 years", r["medicare_note"])
        self.assertIn("Pub. 969", r["hsa_note"])


def _data():
    return {"accounts": [{"name": "Brokerage", "institution": "Fid", "type": "Investments", "subtype": "Brokerage", "balance": 500000.0,
                          "holdings_count": 1, "is_asset": True, "include_nw": True}], "by_account": [], "consolidated": []}


class TestIntegration(unittest.TestCase):
    def run_ret(self, **over):
        cfg = dict(appmod.DEFAULT_RETIRE, birth_year=datetime.date.today().year - 50, annual_expenses_override=60000.0,
                   annual_savings_override=0.0, withdrawal_rate_pct=4.0, healthcare_expense_mode="off")
        cfg.update(over)
        with mock.patch.object(appmod, "_data", return_value=_data()), mock.patch.object(appmod, "_read_retire", return_value=cfg), \
                mock.patch.object(appmod, "_cashflow_annual", return_value=(None, None)):
            return appmod._retirement()

    def test_on_by_default_adds_the_expected_yearly_cost(self):
        self.assertEqual(appmod.DEFAULT_RETIRE["ltc_expense_mode"], "add")
        r = self.run_ret()
        a = r["assumptions"]
        self.assertTrue(a["ltc_line"]["included"])
        self.assertEqual(a["annual_expenses"], round(60000 + a["ltc_line"]["annual"], 2))
        self.assertEqual(a["annual_expenses_base"], 60000.0)
        self.assertEqual(r["fi_number"], round(a["annual_expenses"] / 0.04, 2))

    def test_off_leaves_expenses_alone_and_both_lines_stack_when_on(self):
        off = self.run_ret(ltc_expense_mode="off")
        self.assertEqual(off["assumptions"]["annual_expenses"], 60000.0)
        self.assertFalse(off["assumptions"]["ltc_line"]["included"])
        both = self.run_ret(healthcare_expense_mode="add")
        a = both["assumptions"]
        self.assertEqual(a["annual_expenses"], round(60000 + a["healthcare_line"]["annual"] + a["ltc_line"]["annual"], 2))

    def test_the_bridge_ignores_the_ltc_add_too(self):
        r = self.run_ret(target_age=55)
        self.assertEqual(r["bridge"]["annual_expenses"], 60000.0)

    def test_summary_and_config_and_chat_tool(self):
        s = appmod._ltc_summary(_data(), dict(appmod.DEFAULT_RETIRE, birth_year=datetime.date.today().year - 50))
        self.assertTrue(s["ok"])
        with mock.patch.object(appmod, "_data", return_value=_data()):
            self.assertTrue(appmod._fiduciary_dispatch("get_ltc", {})["ok"])
        self.assertIn("get_ltc", [t["function"]["name"] for t in appmod.FIDUCIARY_TOOLS])
        self.assertIn("get_hsa", [t["function"]["name"] for t in appmod.FIDUCIARY_TOOLS])
        self.assertIn("NOT paid by Medicare", appmod.FIDUCIARY_PROMPT)
        self.assertIn('@app.get("/api/ltc")', APP)
        self.assertIn('ltc_scenario: Optional[Literal["typical_path", "home_care", "assisted_living", "nursing_home", "stress"]]', APP)

    def test_plan_screen_has_the_inputs_and_saves_them(self):
        for needle in ('id="ret-ltcsc"', 'id="ret-ltcage"', 'id="ret-ltcins"', 'id="ret-ltcyrs"', 'id="ret-ltcadd"', "ltc_scenario:", "ltc_expense_mode:",
                       "ltc_insurance_daily: num('ret-ltcins')"):
            self.assertIn(needle, HTML, needle)


if __name__ == "__main__":
    unittest.main()
