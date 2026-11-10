"""Per-account cash designation: the person's word beats the rules; household-wide, admin-only, encrypted; donuts and trace honour it."""
import glob, os, re, sqlite3, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="desig-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import cashdetect as cd  # noqa: E402
import prefs  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()


def acct(name, inst="Voya", typ="Investments", sub="HSA", bal=4000.0, n=0, **kw):
    return dict({"name": name, "institution": inst, "type": typ, "subtype": sub, "balance": bal, "holdings_count": n, "is_asset": True}, **kw)


def K(a):
    return cd.desig_key(a["institution"], a["name"])


class TestRules(unittest.TestCase):
    def setUp(self):
        self.hsa = acct("Voya HSA Cash")
        self.fund = acct("Fidelity Brokerage", inst="Fidelity", sub="Brokerage", bal=10000.0, n=2)
        self.pos = [{"institution": "Fidelity", "account": "Fidelity Brokerage", "ticker": "VTI", "name": "Vanguard Total", "type": "ETF", "value": 10000.0},
                    {"institution": "Fidelity", "account": "Fidelity Brokerage", "ticker": "", "name": "Cash", "type": "Cash", "value": 50.0}]

    def row(self, des, accts=None):
        accts = accts or [self.hsa, self.fund]
        return {r["name"]: r for r in cd.classify_accounts(accts, self.pos, des)}

    def test_without_a_designation_the_hsa_is_a_low_confidence_guess(self):
        r = self.row({})["Voya HSA Cash"]
        self.assertEqual((r["verdict"], r["confidence"]), ("unsure", "low"))

    def test_designation_beats_the_guess_and_is_high_confidence(self):
        for d, verdict in (("investment", "not_cash"), ("brokerage_cash", "brokerage_cash"), ("bank_cash", "bank_cash"), ("exclude", "excluded")):
            r = self.row({K(self.hsa): d})["Voya HSA Cash"]
            self.assertEqual((r["verdict"], r["confidence"], r["designation"]), (verdict, "high", d))
            self.assertTrue(r["reasons"][0].startswith("set by you"))
            self.assertEqual(r["guess"], "unsure")                                  # the rules' opinion is kept for display

    def test_brokerage_cash_designation_feeds_the_cash_slice(self):
        des = {K(self.hsa): "brokerage_cash"}
        cb = cd.cash_breakdown([self.hsa, self.fund], self.pos, des)
        self.assertEqual(cb["brokerage_cash_accounts"], 4000.0)
        self.assertEqual(cb["brokerage_cash"], 4050.0)                                # + the $50 cash line in the brokerage account
        self.assertIn("you designated", cb["account_lines"][0]["why"])
        self.assertEqual(cd.cash_breakdown([self.hsa, self.fund], self.pos, {})["brokerage_cash_accounts"], 0.0)

    def test_exclude_and_bank_cash_leave_a_holdingsless_account_out(self):
        for d in ("exclude", "bank_cash"):
            des = {K(self.hsa): d}
            self.assertEqual([a["name"] for a in cd.left_out_accounts([self.hsa, self.fund], self.pos, des)], ["Voya HSA Cash"])
            self.assertEqual(cd.cash_breakdown([self.hsa, self.fund], self.pos, des)["brokerage_cash_accounts"], 0.0)
        self.assertEqual(cd.left_out_accounts([self.hsa, self.fund], self.pos, {K(self.hsa): "investment"}), [])

    def test_exclude_and_brokerage_cash_are_ignored_on_an_account_with_holdings(self):
        for d in ("exclude", "brokerage_cash"):
            r = self.row({K(self.fund): d})["Fidelity Brokerage"]
            self.assertIsNone(r["designation"])
            self.assertIn("ignored", " ".join(r["reasons"]))
            self.assertEqual(cd.left_out_accounts([self.hsa, self.fund], self.pos, {K(self.fund): d}), [])

    def test_bank_cash_designation_moves_a_cash_line_to_bank_cash(self):
        cb = cd.cash_breakdown([self.hsa, self.fund], self.pos, {K(self.fund): "bank_cash"})
        self.assertEqual(cb["bank_cash"], 50.0)
        self.assertEqual(cb["brokerage_cash"], 0.0)

    def test_report_carries_the_designation_and_counts_it(self):
        rep = cd.report([self.hsa, self.fund], self.pos, 0, 0, {K(self.hsa): "brokerage_cash"})
        r = [x for x in rep["accounts"] if x["name"] == "Voya HSA Cash"][0]
        self.assertTrue(r["counted_in_widgets"])
        self.assertEqual(r["designation"], "brokerage_cash")


class TestStore(unittest.TestCase):
    def test_round_trip_clear_and_validation(self):
        k = prefs.desig_key("Voya", "Voya HSA Cash")
        self.assertEqual(prefs.set_cash_designations({k: "exclude"}), {k: "exclude"})
        self.assertEqual(prefs.cash_designations(), {k: "exclude"})
        self.assertEqual(prefs.set_cash_designations({k: "auto"}), {})
        for bad in ({"nokey": "exclude"}, {k: "bogus"}):
            with self.assertRaises(ValueError):
                prefs.set_cash_designations(bad)

    def test_encrypted_at_rest(self):
        k = prefs.desig_key("Voya", "Secret Account Name")
        prefs.set_cash_designations({k: "investment"})
        raw = sqlite3.connect(prefs.PREFS_DB).execute("SELECT blob FROM household").fetchone()[0]
        self.assertNotIn("Secret Account Name", raw)
        prefs.set_cash_designations({k: None})


class TestWiring(unittest.TestCase):
    def test_endpoints_are_admin_only_and_audited_without_names(self):
        post = APP.split('@app.post("/api/cash-designations")')[1].split("@app.get")[0]
        self.assertIn("_require_admin(request)", post)
        self.assertIn('audit.emit("cashdesignation.updated"', post)
        self.assertIn('detail={"changed": len(ch), "total": len(stored)}', post)
        self.assertNotIn("/api/cash-designations", re.search(r"_VIEWER_ALLOWED_WRITE_PATHS = \{.*?\n\}", APP, re.S).group(0))
        self.assertIn("_require_admin(request)", APP.split('@app.get("/api/cash-designations")')[1].split("@app.post")[0])

    def test_derive_and_trace_use_the_designations(self):
        self.assertIn("_des = prefs.cash_designations()", APP)
        self.assertIn("left_out_accounts(accounts, positions, _des)", APP)
        self.assertIn('"left_out": [', APP)
        self.assertIn("cash_breakdown(accounts, positions, prefs.cash_designations())", APP)

    def test_ui_select_has_every_choice_and_no_inline_handlers(self):
        fn = HTML.split("function designationSelect(a)")[1].split("function cashCheckText")[0]
        for v in ("investment", "brokerage_cash", "bank_cash", "exclude", "Auto"):
            self.assertIn(v, HTML.split("const DESIG_CHOICES=")[1].split("function designationSelect")[0])
        self.assertIn("s.onchange=", fn)
        self.assertNotRegex(fn, r"\bonchange\s*=\s*[\"']")
        self.assertIn("(a.holdings_count||0)>0", fn)                                  # exclude / brokerage cash are disabled with holdings
        self.assertIn("Left out by your setting", HTML)


class TestDerive(unittest.TestCase):
    """The donuts' numbers follow the designation (through the real _derive)."""
    def run_derive(self, des):
        import app as appmod
        hsa = acct("Voya HSA Cash", bal=4000.0, include_nw=True)
        bro = acct("Fid", inst="Fidelity", sub="Brokerage", bal=1000.0, n=1, include_nw=True)
        pos = [{"ticker": "VTI", "name": "VTI", "type": "ETF", "quantity": 1, "value": 1000.0, "cost_total": 900.0, "gain": 100.0, "price": 1000.0,
                "day_pct": 0.1, "institution": "Fidelity", "account": "Fid"}]
        bya = [{"account": "Fid", "institution": "Fidelity", "value": 1000.0, "holdings": pos}]
        prefs.set_cash_designations({cd.desig_key("Voya", "Voya HSA Cash"): des})
        try:
            d = appmod._derive([hsa, bro], [dict(p) for p in pos], bya, [bro], 5000.0, 5000.0, 0.0, balance_only=[hsa])
        finally:
            prefs.set_cash_designations({cd.desig_key("Voya", "Voya HSA Cash"): None})
        return d

    def test_each_designation(self):
        auto = self.run_derive(None)
        self.assertEqual(auto["alloc_display"]["total"], 5000.0)                        # Auto: unchanged from before (balance-only HSA in the institution donut)
        for des in ("exclude", "bank_cash"):
            d = self.run_derive(des)
            self.assertEqual(d["alloc_display"]["total"], 1000.0)
            self.assertEqual([x["label"] for x in d["alloc_display"]["by_institution"]], ["Fidelity"])
            self.assertEqual(d["alloc_display"]["left_out"][0]["value"], 4000.0)
            self.assertEqual(d["brokerage_cash"], 0)
            self.assertEqual(d["net_worth"], 5000.0)                                    # net worth is never changed by a designation
        d = self.run_derive("brokerage_cash")
        self.assertEqual(d["brokerage_cash"], 4000.0)
        self.assertEqual(d["alloc_display"]["total"], 5000.0)                           # in the cash slice and (once) in its institution slice
        self.assertEqual(self.run_derive("investment")["alloc_display"]["total"], 5000.0)


if __name__ == "__main__":
    unittest.main()
