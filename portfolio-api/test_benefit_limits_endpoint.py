import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="limits-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402
import benefits  # noqa: E402


class TestLimits(unittest.TestCase):
    def get(self, age):
        with mock.patch.object(appmod, "_read_retire", return_value={}), mock.patch.object(appmod, "_derive_age", return_value=age):
            return appmod.get_benefit_limits()

    def test_limit_depends_on_age_and_comes_from_the_registry(self):
        L = benefits.LIMITS_2026
        self.assertEqual(self.get(41)["k401_for_you"], L["k401_employee"])
        self.assertEqual(self.get(52)["k401_for_you"], L["k401_employee"] + L["k401_catchup_50"])
        self.assertEqual(self.get(61)["k401_for_you"], L["k401_employee"] + L["k401_catchup_60_63"])
        self.assertEqual(self.get(64)["k401_for_you"], L["k401_employee"] + L["k401_catchup_50"])   # super catch-up ends at 64
        r = self.get(None)
        self.assertEqual(r["k401_for_you"], L["k401_employee"]); self.assertIn("age unknown", r["k401_for_you_basis"])

    def test_carries_year_verified_date_and_source_and_does_no_network(self):
        r = self.get(41)
        self.assertEqual(r["year"], benefits.LIMITS_YEAR); self.assertTrue(r["verified"]); self.assertTrue(r["source_url"].startswith("https://www.irs.gov"))
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read().split("def get_benefit_limits():")[1].split("# ============ GAP-2")[0]
        self.assertNotIn("requests.", src); self.assertNotIn("_tavily", src)


if __name__ == "__main__":
    unittest.main()
