"""Medical reserve in the asset-type donut + the HEALTHCARE RESERVE tracker tile (2026-11-12)."""
import datetime, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="medres-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import cashdetect as cd  # noqa: E402
import prefs  # noqa: E402
import app as appmod  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()


def acct(name, inst, sub, bal, n=0, **kw):
    return dict({"name": name, "institution": inst, "type": "Investments", "subtype": sub, "balance": bal, "holdings_count": n, "is_asset": True,
                 "include_nw": True, "id": "id_" + name}, **kw)


class TestSlice(unittest.TestCase):
    def derive(self, des=None):
        hsa = acct("Health Savings Account (...8095)", "Voya", "Health Savings Account (HSA)", 3418.47)
        old = acct("Health Savings Account (...6838)", "Voya", "Other", 0.0)
        bro = acct("Fid", "Fidelity", "Brokerage", 1000.0, 1)
        pos = [{"ticker": "VTI", "name": "VTI", "type": "ETF", "quantity": 1, "value": 1000.0, "cost_total": 900.0, "gain": 100.0, "price": 1000.0,
                "day_pct": 0.1, "institution": "Fidelity", "account": "Fid"}]
        bya = [{"account": "Fid", "institution": "Fidelity", "value": 1000.0, "holdings": pos}]
        if des:
            prefs.set_cash_designations({cd.desig_key("Voya", hsa["name"], hsa["id"]): des})
        try:
            return appmod._derive([hsa, old, bro], [dict(p) for p in pos], bya, [bro], 5000.0, 5000.0, 0.0, balance_only=[hsa])
        finally:
            if des:
                prefs.set_cash_designations({cd.desig_key("Voya", hsa["name"], hsa["id"]): None})

    def test_auto_gives_a_named_slice_and_the_two_donuts_tie(self):
        d = self.derive()
        t = {r["label"]: r["value"] for r in d["alloc_display"]["by_type"]}
        self.assertEqual(t["Medical reserve (HSA)"], 3418.47)
        self.assertEqual(d["alloc_display"]["medical_reserve"], 3418.47)
        self.assertEqual(d["alloc_display"]["type_total"], d["alloc_display"]["total"])           # asset-type donut = institution donut (no cash accounts here)
        self.assertEqual(d["hsa_balance"], 3418.47)
        self.assertEqual({r["label"]: r["value"] for r in d["by_type"]}, {"ETF": 1000.0})            # engines never see the slice

    def test_the_persons_designation_takes_the_medical_slice_away(self):
        for des in ("exclude", "bank_cash", "investment", "brokerage_cash"):
            d = self.derive(des)
            t = {r["label"] for r in d["alloc_display"]["by_type"]}
            self.assertNotIn("Medical reserve (HSA)", t, des)
            self.assertEqual(d["alloc_display"]["medical_reserve"], 0.0, des)
            self.assertEqual(d["hsa_balance"], 3418.47, des)                                           # the tracker tile still knows the HSA exists

    def test_w1_the_two_donuts_tie_under_every_designation(self):
        """Cowork retest 2026-11-12: Investment on the balance-only HSA dropped it from the asset-type donut but not the institution donut."""
        for des in (None, "investment", "brokerage_cash", "exclude", "bank_cash"):
            ad = self.derive(des)["alloc_display"]
            self.assertEqual(ad["type_total"], ad["total"], des)
        inv = self.derive("investment")["alloc_display"]
        self.assertEqual({r["label"]: r["value"] for r in inv["by_type"]}["Investments (no holdings reported)"], 3418.47)
        self.assertEqual(inv["untyped_investments"], 3418.47)
        self.assertEqual(self.derive()["alloc_display"]["untyped_investments"], 0.0)                    # Auto: the HSA is the medical reserve instead
        self.assertNotIn("Investments (no holdings reported)", {r["label"] for r in self.derive("exclude")["alloc_display"]["by_type"]})

    def test_engines_never_see_the_untyped_slice(self):
        d = self.derive("investment")
        self.assertEqual({r["label"]: r["value"] for r in d["by_type"]}, {"ETF": 1000.0})


class TestSummary(unittest.TestCase):
    def test_hsa_summary_carries_the_tracker_numbers(self):
        data = {"accounts": [acct("HSA", "Voya", "HSA", 3418.47)], "by_account": [], "consolidated": []}
        cfg = {"birth_year": datetime.date.today().year - 50, "healthcare_need_override": None}
        s = appmod._hsa_summary(data, cfg)
        self.assertEqual(s["annual_cost"]["annual"], 7420.0)
        self.assertEqual(s["years_covered"], round(3418.47 / 7420.0, 1))
        self.assertEqual(s["pct_of_need"], round(3418.47 / 185500.0 * 100, 1))
        cfg["healthcare_need_override"] = 100000.0
        s = appmod._hsa_summary(data, cfg)
        self.assertEqual((s["annual_cost"]["annual"], s["pct_of_need"]), (4000.0, 3.4))


class TestTile(unittest.TestCase):
    def test_tile_is_registered_ordered_labelled_and_hidden_without_an_hsa(self):
        for needle in ("health:()=>healthPanel(d)", "'alloc-type','health','mix-hold'", "health:'HEALTHCARE RESERVE'", "if(!(d.hsa_balance>0)) return null;",
                       "fetch('/api/hsa')", "fetch('/api/ltc')", "role','progressbar'"):
            self.assertIn(needle, HTML, needle)
        self.assertIn('"hsa_balance": hsa_balance', APP)

    def test_a_saved_dashboard_order_without_the_new_tile_still_gets_it(self):
        body = HTML.split("function dashOrderParse(raw){")[1].split("function dashMergeOrder")[0]
        self.assertIn("known.filter(k=>seen.indexOf(k)<0)", body)                                      # unknown-to-the-saved-order defaults are appended


if __name__ == "__main__":
    unittest.main()
