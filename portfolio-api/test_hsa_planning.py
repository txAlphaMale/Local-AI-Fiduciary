"""HSA planning: medical reserve vs retirement surplus (hsa.py), the dated healthcare-need constant, /api/hsa and the retirement integration."""
import datetime, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="hsaplan-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import hsa  # noqa: E402
import app as appmod  # noqa: E402

TODAY = datetime.date(2026, 11, 10)


class TestSplit(unittest.TestCase):
    def test_small_hsa_is_all_medical_reserve(self):
        s = hsa.split(3400, 50, 185500)
        self.assertEqual((s["reserve"], s["surplus"], s["counted_for_retirement"]), (3400.0, 0.0, 0.0))

    def test_surplus_is_locked_before_65_and_counted_from_65(self):
        young = hsa.split(250000, 58, 185500)
        self.assertEqual((young["reserve"], young["surplus"], young["counted_for_retirement"], young["locked_until_65"]), (185500.0, 64500.0, 0.0, 64500.0))
        old = hsa.split(250000, 65, 185500)
        self.assertEqual((old["counted_for_retirement"], old["locked_until_65"]), (64500.0, 0.0))

    def test_unknown_age_or_need_counts_nothing_as_retirement_money(self):
        self.assertEqual(hsa.split(250000, None, 185500)["counted_for_retirement"], 0.0)
        s = hsa.split(250000, 70, None)
        self.assertEqual((s["reserve"], s["counted_for_retirement"]), (250000.0, 0.0))

    def test_zero_and_negative_balances(self):
        self.assertEqual(hsa.split(0, 70, 1000)["reserve"], 0.0)
        self.assertEqual(hsa.split(-5, 70, 1000)["balance"], 0.0)


class TestNeed(unittest.TestCase):
    def test_the_estimate_comes_from_the_dated_table_and_scales_with_people(self):
        t = hsa.table(TODAY)
        self.assertEqual(t["per_person"], 185500.0)
        self.assertEqual(t["year"], 2026)
        self.assertIn("fidelity.com", t["source_url"])
        self.assertEqual(hsa.need([50], today=TODAY)["total"], 185500.0)
        self.assertEqual(hsa.need([50, 48], today=TODAY)["total"], 371000.0)          # a couple is twice the single figure
        self.assertEqual(hsa.need([50, 48], today=TODAY)["people"], 2)

    def test_need_shrinks_through_retirement(self):
        self.assertEqual(hsa.remaining_fraction(None), 1.0)
        self.assertEqual(hsa.remaining_fraction(65), 1.0)
        self.assertAlmostEqual(hsa.remaining_fraction(77.5), 0.5)
        self.assertEqual(hsa.remaining_fraction(95), 0.0)
        self.assertLess(hsa.need([80], today=TODAY)["total"], hsa.need([66], today=TODAY)["total"])

    def test_override_replaces_the_estimate(self):
        n = hsa.need([50], override=120000, today=TODAY)
        self.assertEqual((n["total"], n["source"]), (120000.0, "your override"))

    def test_registry_lists_the_constant_and_the_table_value_is_sourced(self):
        import grounding
        r = [x for x in grounding.registry() if x["key"] == "hsa.healthcare_need"]
        self.assertEqual(len(r), 1)
        self.assertNotIn("error", r[0])
        self.assertTrue(r[0]["source_url"])


def _data_with(hsa_balance=3400.0, other=100000.0):
    return {"accounts": [
        {"name": "Voya HSA Cash", "institution": "Voya", "type": "Investments", "subtype": "HSA", "balance": hsa_balance, "holdings_count": 0,
         "is_asset": True, "include_nw": True},
        {"name": "Brokerage", "institution": "Fid", "type": "Investments", "subtype": "Brokerage", "balance": other, "holdings_count": 1,
         "is_asset": True, "include_nw": True}], "by_account": [], "consolidated": []}


class TestSummaryAndRetirement(unittest.TestCase):
    def test_summary_for_a_small_hsa_is_all_reserve(self):
        cfg = {"birth_year": datetime.date.today().year - 50, "healthcare_need_override": None}
        s = appmod._hsa_summary(_data_with(), cfg)
        self.assertEqual((s["balance"], s["reserve"], s["counted_for_retirement"]), (3400.0, 3400.0, 0.0))
        self.assertEqual(s["accounts"][0]["account"], "Voya HSA Cash")

    def test_override_and_age_make_a_surplus_count_from_65(self):
        yr = datetime.date.today().year
        s = appmod._hsa_summary(_data_with(50000.0), {"birth_year": yr - 66, "healthcare_need_override": 20000})
        self.assertEqual((s["reserve"], s["surplus"], s["counted_for_retirement"]), (20000.0, 30000.0, 30000.0))
        s = appmod._hsa_summary(_data_with(50000.0), {"birth_year": yr - 50, "healthcare_need_override": 20000})
        self.assertEqual((s["counted_for_retirement"], s["locked_until_65"]), (0.0, 30000.0))

    def test_retirement_leaves_the_hsa_reserve_out_of_investable_assets(self):
        cfg = dict(appmod.DEFAULT_RETIRE, birth_year=datetime.date.today().year - 50, annual_expenses_override=60000.0, annual_savings_override=0.0)
        with mock.patch.object(appmod, "_data", return_value=_data_with()), mock.patch.object(appmod, "_read_retire", return_value=cfg), \
                mock.patch.object(appmod, "_cashflow_annual", return_value=(None, None)):
            r = appmod._retirement()
        self.assertEqual(r["investable_assets"], 100000.0)                          # the $3,400 HSA is a medical reserve, not retirement money
        self.assertEqual(r["hsa"]["reserve"], 3400.0)

    def test_api_and_config_wiring(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn('@app.get("/api/hsa")', src)
        self.assertIn("healthcare_need_override: Optional[float]", src)
        self.assertIn('"healthcare_need_override": None', src)


if __name__ == "__main__":
    unittest.main()


class TestHealthcareExpenseLine(unittest.TestCase):
    def test_annual_cost_spreads_the_lifetime_estimate_over_65_to_90(self):
        c = hsa.annual_cost(1, today=TODAY)
        self.assertEqual((c["annual"], c["years"]), (7420.0, 25))                      # 185,500 / 25
        self.assertEqual(hsa.annual_cost(2, today=TODAY)["annual"], 14840.0)
        self.assertEqual(hsa.annual_cost(2, override=250000, today=TODAY)["annual"], 10000.0)   # an override is a household lifetime total
        self.assertEqual(hsa.annual_cost(2, override=250000, today=TODAY)["source"], "your override")

    def run_ret(self, **cfg_over):
        cfg = dict(appmod.DEFAULT_RETIRE, birth_year=datetime.date.today().year - 50, annual_expenses_override=60000.0,
                   annual_savings_override=0.0, withdrawal_rate_pct=4.0, **cfg_over)
        with mock.patch.object(appmod, "_data", return_value=_data_with()), mock.patch.object(appmod, "_read_retire", return_value=cfg), \
                mock.patch.object(appmod, "_cashflow_annual", return_value=(None, None)):
            return appmod._retirement()

    def test_on_by_default_and_the_default_is_a_dated_decision(self):
        self.assertEqual(appmod.DEFAULT_RETIRE["healthcare_expense_mode"], "add")
        r = self.run_ret()
        a = r["assumptions"]
        self.assertTrue(a["healthcare_line"]["included"])
        self.assertEqual(r["fi_number"], round(67420 / 0.04, 2))
        self.assertEqual((a["healthcare_line"]["fi_number_without"], a["healthcare_line"]["fi_number_with"]), (1500000.0, 1685500.0))

    def test_turning_it_off_leaves_the_fi_number_on_plain_expenses(self):
        r = self.run_ret(healthcare_expense_mode="off")
        a = r["assumptions"]
        self.assertEqual(r["fi_number"], 1500000.0)                                     # 60,000 / 4%
        self.assertEqual(a["annual_expenses"], 60000.0)
        self.assertFalse(a["healthcare_line"]["included"])

    def test_turning_it_on_adds_the_yearly_cost_to_expenses_and_the_fi_number(self):
        r = self.run_ret(healthcare_expense_mode="add")
        a = r["assumptions"]
        self.assertEqual((a["annual_expenses"], a["annual_expenses_base"]), (67420.0, 60000.0))
        self.assertEqual(r["fi_number"], round(67420 / 0.04, 2))
        self.assertTrue(a["healthcare_line"]["included"])

    def test_an_override_changes_the_yearly_line(self):
        r = self.run_ret(healthcare_expense_mode="add", healthcare_need_override=100000.0)
        self.assertEqual(r["assumptions"]["annual_expenses"], 64000.0)                  # 100,000 / 25 = 4,000

    def test_config_accepts_only_off_or_add_and_the_plan_screen_has_the_inputs(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        self.assertIn('healthcare_expense_mode: Optional[Literal["off", "add"]]', src)
        html = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
        for needle in ('id="ret-hc"', 'id="ret-hcadd"', "healthcare_need_override: num('ret-hc')", "healthcare_expense_mode:"):
            self.assertIn(needle, html, needle)
        self.assertNotIn("esc(a.healthcare_line", html.replace("escHtml(a.healthcare_line", ""))   # esc() is not in scope in planContent
