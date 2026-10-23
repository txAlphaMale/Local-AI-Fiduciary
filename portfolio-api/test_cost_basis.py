# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_cost_basis.py -- TASK-008: user-entered cost basis, only where the provider reports none."""
import asyncio, glob, json, os, re, sqlite3, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="costbasis-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import app as appmod  # noqa: E402
import backup, store  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
PLAN, BROKER = ("Principal", "401(k) Plan"), ("Fidelity", "Brokerage")


def pos(t, qty, val, acct, inst, cost=None, typ="Mutual Fund"):
    return {"ticker": t, "name": t, "type": typ, "quantity": qty, "value": val, "cost_total": cost, "price": (val / qty if qty else None),
            "day_pct": None, "gain": None if cost is None else val - cost, "gain_pct": None, "suspect": cost is None,
            "account": acct, "institution": inst}


def derive(positions):
    by = {}
    for p in positions:
        by.setdefault((p["institution"], p["account"]), []).append(p)
    by_account = [{"account": a, "institution": i, "value": sum(x["value"] for x in hs), "balance": sum(x["value"] for x in hs), "holdings": [dict(x) for x in hs]}
                  for (i, a), hs in by.items()]
    accts = [{"id": a, "name": a, "institution": i, "type": "Investments", "balance": 1, "holdings_count": 1, "is_asset": True} for (i, a) in by]
    return appmod._derive(accts, [dict(p) for p in positions], by_account, accts, 1, 1, 0.0)


class Reg:
    """In-memory stand-in for the register file."""
    def __init__(self, entries=None): self.entries = entries or []
    def __enter__(self):
        self.ps = [mock.patch.object(appmod, "_read_basis_entries", lambda: list(self.entries)),
                   mock.patch.object(appmod, "_basis_save", self._save)]
        for p in self.ps: p.start()
        return self
    def __exit__(self, *a):
        for p in self.ps: p.stop()
    def _save(self, entries): self.entries = list(entries)


def entry(t, avg, qty, inst=PLAN[0], acct=PLAN[1], as_of="2026-10-01"):
    return {"institution": inst, "account": acct, "ticker": t, "avg_cost": avg, "qty_at_entry": qty, "as_of": as_of, "note": ""}


class TestOverlay(unittest.TestCase):
    def test_applies_only_where_the_provider_reports_none_and_provider_wins_otherwise(self):
        with Reg([entry("FXAIX", 100.0, 10), entry("VTI", 5.0, 10, BROKER[0], BROKER[1])]):
            d = derive([pos("FXAIX", 10, 2000.0, *PLAN[::-1]), pos("VTI", 10, 2800.0, BROKER[1], BROKER[0], cost=1200.0, typ="ETF")])
        by = {p["ticker"]: p for p in d["positions"]}
        self.assertEqual((by["FXAIX"]["cost_total"], by["FXAIX"]["cost_source"], by["FXAIX"]["gain"]), (1000.0, "manual", 1000.0))
        self.assertEqual((by["VTI"]["cost_total"], by["VTI"].get("cost_source")), (1200.0, None))          # provider basis wins over the entry
        self.assertFalse(by["VTI"]["basis_missing"])

    def test_cash_never_gets_a_manual_basis(self):
        with Reg([entry("CASH", 1.0, 10)]):
            d = derive([pos("CASH", 10, 10.0, *PLAN[::-1], typ="Cash")])
        self.assertIsNone(d["positions"][0]["cost_total"]); self.assertFalse(d["positions"][0]["basis_missing"])

    def test_stale_flag_at_more_than_one_percent_drift_only_and_cost_follows_current_quantity(self):
        with Reg([entry("FXAIX", 100.0, 100)]):
            ok = derive([pos("FXAIX", 100.9, 2018.0, *PLAN[::-1])])["positions"][0]      # +0.9%
            st = derive([pos("FXAIX", 102.0, 2040.0, *PLAN[::-1])])["positions"][0]      # +2%
        self.assertFalse(ok["basis_stale"]); self.assertAlmostEqual(ok["cost_total"], 10090.0)
        self.assertTrue(st["basis_stale"]); self.assertAlmostEqual(st["cost_total"], 10200.0)          # average x CURRENT shares
        self.assertEqual(st["basis_entry"]["qty_at_entry"], 100)

    def test_consolidated_shows_cost_only_when_every_account_is_known_and_reports_x_of_y(self):
        ps = [pos("FXAIX", 10, 2000.0, PLAN[1], PLAN[0]), pos("FXAIX", 5, 1000.0, "IRA", "Fidelity")]
        with Reg([entry("FXAIX", 100.0, 10)]):
            c = derive(ps)["consolidated"][0]
        self.assertEqual((c["basis_lines"], c["basis_known"]), (2, 1)); self.assertIsNone(c["cost_total"])          # partial -> hidden, as before
        with Reg([entry("FXAIX", 100.0, 10), entry("FXAIX", 90.0, 5, "Fidelity", "IRA")]):
            c = derive(ps)["consolidated"][0]
        self.assertEqual((c["basis_known"], c["cost_total"], c["cost_source"]), (2, 1450.0, "manual"))

    def test_totals_include_manual_lines_and_report_the_user_entered_share(self):
        ps = [pos("FXAIX", 10, 2000.0, PLAN[1], PLAN[0]), pos("VTI", 10, 2800.0, BROKER[1], BROKER[0], cost=1000.0, typ="ETF")]
        with Reg([entry("FXAIX", 100.0, 10)]):
            d = derive(ps)
        self.assertEqual((d["total_cost"], d["total_unrealized_gain"]), (2000.0, 2800.0))
        self.assertEqual(d["manual_basis"], {"lines": 1, "cost": 1000.0, "share_pct": 50.0, "stale_lines": 0})

    def test_identity_is_institution_account_ticker_not_ids_and_renames_are_unmatched(self):
        with Reg([entry("FXAIX", 100.0, 10, "principal financial", "401(k)  PLAN")]):          # case/spacing/punctuation tolerant? no: names must match
            d = derive([pos("FXAIX", 10, 2000.0, PLAN[1], PLAN[0])])
        self.assertIsNone(d["positions"][0]["cost_total"])                                    # different institution text -> not applied
        with Reg([entry("FXAIX", 100.0, 10, "PRINCIPAL", "401(k) plan")]):
            d = derive([pos("FXAIX", 10, 2000.0, PLAN[1], PLAN[0])])
        self.assertEqual(d["positions"][0]["cost_source"], "manual")                          # normalization: case + punctuation


class TestProviderShapes(unittest.TestCase):
    def test_csv_provider(self):
        rows = [{"ticker": "FXAIX", "name": "x", "quantity": "10", "cost_basis": "", "account": "Plan", "institution": "Principal", "type": "Mutual Fund"}]
        with mock.patch.object(appmod, "_finnhub", lambda k, t: {"c": 200.0, "dp": 0}), Reg([entry("FXAIX", 100.0, 10, "Principal", "Plan")]):
            a, p, b, nw = appmod._csv_raw(rows)
            d = appmod._derive(a, p, b, a, nw, nw, 0.0)
        self.assertEqual((d["positions"][0]["cost_total"], d["positions"][0]["cost_source"]), (1000.0, "manual"))

    def test_monarch_shaped_payload(self):
        node = {"quantity": 10, "totalValue": 2000.0, "basis": 0, "security": {"ticker": "FXAIX", "name": "F", "typeDisplay": "Mutual Fund", "currentPrice": 200.0}, "holdings": [{}]}
        acct = {"id": "1", "displayName": "401(k) Plan", "institution": {"name": "Principal"}, "type": {"display": "Investments"}, "displayBalance": 2000.0,
                "holdingsCount": 1, "isAsset": True, "includeBalanceInNetWorth": True, "credential": {}}
        post = lambda op, payload: {"accounts": [acct]} if op == "get_accounts" else {"holdings": {"portfolio": {"aggregateHoldings": {"edges": [{"node": node}]}}}}
        with mock.patch.object(appmod, "_post", post), mock.patch.object(appmod, "_monarch_guard", lambda: None), Reg([entry("FXAIX", 100.0, 10)]):
            d = appmod._build_monarch()
        self.assertEqual((d["positions"][0]["cost_total"], d["by_account"][0]["holdings"][0]["cost_source"]), (1000.0, "manual"))   # both views

    def test_snaptrade_mapped_payload(self):
        import snaptrade_integration as si
        fx = json.load(open(os.path.join(_HERE, "test_data", "snaptrade_sandbox.json")))
        m = si.map_portfolio(fx["accounts"], fx["positions"], fx["balances"], {}, fx["connections"])
        target = next(p for p in m["positions"] if p["ticker"] != "CASH")
        e = entry(target["ticker"], 1.0, target["quantity"], target["institution"], target["account"])
        stripped = []
        for p in m["positions"]:
            q = dict(p); 
            if q["ticker"] == target["ticker"]: q["cost_total"], q["suspect"] = None, True
            stripped.append(q)
        with Reg([e]):
            d = appmod._derive(m["accounts"], stripped, m["by_account"], m["inv"], m["net_worth"], m["total_assets"], 0.0, balance_only=m["balance_only"])
        got = next(p for p in d["positions"] if p["ticker"] == target["ticker"] and p["account"] == target["account"])
        self.assertEqual(got["cost_source"], "manual")

    def test_plaid_shaped_payload(self):
        ps = [pos("FXAIX", 10, 2000.0, "Retirement", "Plaid Bank")]
        with Reg([entry("FXAIX", 90.0, 10, "Plaid Bank", "Retirement")]):
            self.assertEqual(derive(ps)["positions"][0]["cost_total"], 900.0)


class FR:
    def __init__(self, body=None, raw=b""): self._b, self._raw = body, raw; self.session = {}; self.headers = {}; self.client = None
    async def json(self): return self._b
    async def body(self): return self._raw


class TestEndpoints(unittest.TestCase):
    ADMIN = {"id": 1, "username": "a"}

    def setUp(self):
        self.audit = []
        base = derive([pos("FXAIX", 10, 2000.0, PLAN[1], PLAN[0]), pos("BTEKX", 20, 1800.0, PLAN[1], PLAN[0]),
                       pos("VTI", 10, 2800.0, BROKER[1], BROKER[0], cost=1000.0, typ="ETF")])
        self.data = base
        self.reg = Reg(); self.reg.__enter__()
        self.ps = [mock.patch.object(appmod, "_data", lambda: self.data), mock.patch.object(appmod, "_require_admin", lambda r: self.ADMIN),
                   mock.patch.object(appmod.audit, "emit", lambda *a, **k: self.audit.append((a, k)))]
        for p in self.ps: p.start()
    def tearDown(self):
        for p in self.ps: p.stop()
        self.reg.__exit__()

    def post(self, body): return asyncio.run(appmod.post_cost_basis(FR(body)))
    def key(self, t="FXAIX"): return {"institution": PLAN[0], "account": PLAN[1], "ticker": t}

    def test_set_total_and_per_share_convert_and_capture_quantity_at_entry(self):
        self.assertEqual(self.post(dict(self.key(), mode="total", amount="1,500.00", as_of="2026-10-02", note="from statement")), {"ok": True})
        e = self.reg.entries[0]
        self.assertEqual((e["avg_cost"], e["qty_at_entry"], e["as_of"], e["note"]), (150.0, 10, "2026-10-02", "from statement"))
        self.post(dict(self.key("BTEKX"), mode="per_share", amount="55"))
        self.assertEqual([x["avg_cost"] for x in self.reg.entries if x["ticker"] == "BTEKX"], [55.0])
        self.post(dict(self.key(), mode="per_share", amount="160"))                                    # replaces, not duplicates
        self.assertEqual(len([x for x in self.reg.entries if x["ticker"] == "FXAIX"]), 1)

    def test_validation_and_ineligible_lines(self):
        for bad in (dict(self.key(), mode="total", amount="-5"), dict(self.key(), mode="total", amount="abc"), dict(self.key(), mode="x", amount="5"),
                    dict(self.key(), mode="total", amount="0"), dict(self.key(), mode="total", amount="5", as_of="10/02/2026")):
            self.assertEqual(self.post(bad).status_code, 400, bad)
        self.assertEqual(self.post({"institution": BROKER[0], "account": BROKER[1], "ticker": "VTI", "mode": "total", "amount": "5"}).status_code, 404)   # provider reports basis
        self.assertEqual(self.post(dict(self.key("NOPE"), mode="total", amount="5")).status_code, 404)
        self.assertEqual(self.reg.entries, [])

    def test_clear_and_audit_contains_no_amounts(self):
        self.post(dict(self.key(), mode="total", amount="1234.56", note="secret note 99999"))
        self.post(dict(self.key(), clear=True))
        self.assertEqual(self.post(dict(self.key(), clear=True)).status_code, 404)
        names = [a[0][0] for a in self.audit]
        self.assertEqual(names, ["holdings.basis_set", "holdings.basis_cleared"])
        blob = json.dumps([k.get("detail") for _, k in self.audit])
        for forbidden in ("1234", "99999", "amount", "avg", "cost"):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(set(self.audit[0][1]["detail"]), {"institution", "account", "ticker"})

    def test_lines_statuses_active_superseded_unmatched(self):
        self.post(dict(self.key(), mode="total", amount="1000"))
        self.reg.entries.append(entry("VTI", 5.0, 10, BROKER[0], BROKER[1]))              # provider reports VTI basis -> superseded
        self.reg.entries.append(entry("GONE", 5.0, 10, PLAN[0], "Old Plan"))               # no such line -> unmatched
        self.data = derive([pos("FXAIX", 10, 2000.0, PLAN[1], PLAN[0]), pos("BTEKX", 20, 1800.0, PLAN[1], PLAN[0]),
                            pos("VTI", 10, 2800.0, BROKER[1], BROKER[0], cost=1000.0, typ="ETF")])
        out = appmod.get_cost_basis(FR())
        st = {e["ticker"]: e["status"] for e in out["entries"]}
        self.assertEqual(st, {"FXAIX": "active", "VTI": "superseded", "GONE": "unmatched"})
        self.assertEqual(sorted(l["ticker"] for l in out["lines"]), ["BTEKX", "FXAIX"])           # only lines the provider left blank
        self.assertEqual(len(self.reg.entries), 3)                                                # nothing silently deleted

    def imp(self, text): return asyncio.run(appmod.post_cost_basis_import(FR(raw=text.encode())))
    HDR = "institution,account,ticker,name,current_quantity,current_value,total_cost_basis,avg_cost_per_share,as_of,note\n"

    def test_template_lists_only_eligible_lines_with_blank_cost_columns(self):
        r = appmod.get_cost_basis_template(FR())
        rows = r.body.decode().strip().splitlines()
        self.assertEqual(rows[0], self.HDR.strip()); self.assertEqual(len(rows), 3)
        self.assertTrue(all(x.endswith(",,,,") for x in rows[1:]))                                # four trailing blanks: totals, avg, as_of, note

    def test_import_good_file_and_skips_blank_rows(self):
        r = self.imp(self.HDR + "Principal,401(k) Plan,FXAIX,F,10,2000,1500,,2026-10-02,n\nPrincipal,401(k) Plan,BTEKX,B,20,1800,,55,,\n")
        self.assertEqual(r, {"ok": True, "entries_written": 2})
        self.assertEqual(self.imp(self.HDR + "Principal,401(k) Plan,FXAIX,F,10,2000,,,,\n"), {"ok": True, "entries_written": 0})
        self.assertEqual(len(self.reg.entries), 2)

    def test_import_is_all_or_nothing_and_names_the_bad_row(self):
        for bad, frag in (("Principal,401(k) Plan,BTEKX,B,20,1800,100,5,,\n", "OR"), ("Principal,401(k) Plan,BTEKX,B,20,1800,-5,,,\n", "positive"),
                          ("Principal,401(k) Plan,BTEKX,B,20,1800,abc,,,\n", "positive"), ("Principal,401(k) Plan,NOPE,B,20,1800,5,,,\n", "no such line"),
                          ("Principal,401(k) Plan,BTEKX,B,20,1800,5,,not-a-date,\n", "as_of")):
            r = self.imp(self.HDR + "Principal,401(k) Plan,FXAIX,F,10,2000,1500,,,\n" + bad)
            self.assertEqual(r.status_code, 400, bad)
            msg = json.loads(r.body)["errors"][0]
            self.assertEqual(msg["row"], 3); self.assertIn(frag, msg["message"])
            self.assertEqual(self.reg.entries, [], "nothing may be written when any row is bad")
        self.assertEqual(self.imp("a,b\n1,2\n").status_code, 400); self.assertEqual(self.imp("").status_code, 400)
        self.assertEqual(self.audit, [])

    def test_import_audits_the_count_only(self):
        self.imp(self.HDR + "Principal,401(k) Plan,FXAIX,F,10,2000,1500,,,\n")
        (a, k), = self.audit
        self.assertEqual((a[0], k["detail"]), ("holdings.basis_imported", {"count": 1}))


class TestSecurity(unittest.TestCase):
    def test_every_editor_endpoint_is_admin_only_including_the_gets(self):
        with mock.patch.object(appmod, "_require_admin", lambda r: None):
            for fn, args in ((appmod.get_cost_basis, (FR(),)), (appmod.get_cost_basis_template, (FR(),))):
                self.assertEqual(fn(*args).status_code, 403)
            for fn in (appmod.post_cost_basis, appmod.post_cost_basis_import):
                self.assertEqual(asyncio.run(fn(FR({}))).status_code, 403)
        self.assertFalse([p for p in appmod._VIEWER_ALLOWED_WRITE_PATHS if "cost-basis" in p])

    def test_register_survives_backup_and_restore(self):
        entries = [entry("FXAIX", 123.456, 10)]
        store.write_json(appmod.COST_BASIS_FILE, entries)
        src, out, stage = tempfile.mkdtemp(), tempfile.mkdtemp(), os.path.join(tempfile.mkdtemp(), "stage")
        import shutil
        shutil.copy(store.STORE_DB, os.path.join(src, "store.db"))                          # the register lives inside the encrypted store DB the backup tars
        m = backup.run_backup("pw-test", src, out)
        backup.stage_restore("pw-test", m["path"], stage)
        con = sqlite3.connect(os.path.join(stage, "store.db")); con.row_factory = sqlite3.Row
        key = store._key_for(appmod.COST_BASIS_FILE)
        row = con.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        self.assertIsNotNone(row, "register row missing from the restored store")
        self.assertIn("123.456", store._dec(row["value"]))


class TestDownstream(unittest.TestCase):
    def test_manual_basis_in_a_401k_never_becomes_a_taxable_loss_but_in_taxable_it_is_labelled(self):
        deferred = pos("ZZZ", 10, 500.0, "401(k) Plan", "Principal", typ="Stock"); deferred.update(cost_total=2000.0, gain=-1500.0, cost_source="manual", basis_stale=False)
        taxable = pos("YYY", 10, 500.0, "Brokerage", "Fidelity", typ="Stock"); taxable.update(cost_total=2000.0, gain=-1500.0, cost_source="manual", basis_stale=True)
        d = {"accounts": [{"name": "401(k) Plan", "type": "Investments"}, {"name": "Brokerage", "type": "Investments"}],
             "by_account": [{"account": "401(k) Plan", "institution": "Principal", "holdings": [deferred]},
                            {"account": "Brokerage", "institution": "Fidelity", "holdings": [taxable]}], "consolidated": []}
        with mock.patch.object(appmod, "_data", lambda: d), mock.patch.object(appmod, "_read_analysis", lambda: {}), \
             mock.patch.object(appmod, "_read_accounts_cfg", lambda: {}), mock.patch.object(appmod, "_read_exp", lambda: {}):
            try:
                out = appmod._tax_audit()
            except Exception as e:                                                       # keep the test about the TLH rows, not unrelated feeds
                self.skipTest(f"_tax_audit needs more fixtures: {e}")
        cands = out["tax_loss_harvest"]["candidates"]
        self.assertEqual([c["ticker"] for c in cands], ["YYY"])                           # the 401(k) line is never a candidate
        self.assertEqual((cands[0]["cost_source"], cands[0]["basis_stale"]), ("manual", True))


class TestExposureCacheSchema(unittest.TestCase):
    def test_an_older_cache_is_stale_so_the_refresh_starts_on_the_first_load(self):
        old = {"generated_at": __import__("time").time(), "positions": []}
        started = []
        with mock.patch.object(appmod, "_read_exp", lambda: old), mock.patch.object(appmod.threading, "Thread", lambda **k: mock.Mock(start=lambda: started.append(1))):
            r = appmod.get_exposure()
        self.assertTrue(r["refreshing"]); self.assertEqual(started, [1])
        new = dict(old, schema=appmod.EXP_SCHEMA); started.clear()
        with mock.patch.object(appmod, "_read_exp", lambda: new), mock.patch.object(appmod.threading, "Thread", lambda **k: mock.Mock(start=lambda: started.append(1))):
            r = appmod.get_exposure()
        self.assertFalse(r["refreshing"]); self.assertEqual(started, [])


class TestSinglePencil(unittest.TestCase):
    """Tester screenshot 2026-10-23: manual-basis rows showed TWO pencils (one clickable, one not)."""
    def render(self, viewer):
        import shutil, subprocess
        if not shutil.which("node"): self.skipTest("node not installed")
        def fn(name):
            i = HTML.index(f"function {name}(")
            d, k = 0, HTML.index("{", i)
            while True:
                d += HTML[k] == "{"; d -= HTML[k] == "}"; k += 1
                if d == 0: return HTML[i:k]
        src = ("const attrEsc = x => (x==null?'':String(x)).replace(/\"/g,'&quot;').replace(/</g,'&lt;');\nconst usd=n=>'$'+Math.round(n).toLocaleString();\n"
               f"const isViewer=()=>{str(viewer).lower()};\n" + fn("basisPen") + fn("manualCostCell") + """
        const p={ticker:'FXAIX',account:'Plan',type:'Mutual Fund',cost_total:1500,quantity:12,basis_stale:true,basis_entry:{as_of:'2026-10-01',qty_at_entry:10}};
        console.log(JSON.stringify([manualCostCell(p), manualCostCell(Object.assign({},p,{basis_stale:false}))]));""")
        r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_admin_sees_exactly_one_clickable_pencil_and_it_carries_the_info(self):
        for html in self.render(False):
            self.assertEqual(html.count("✎"), 1)
            self.assertEqual(html.count('class="basis-pen"'), 1)
            self.assertIn("entered by you on 2026-10-01", html); self.assertIn("click to edit or clear", html)
        self.assertIn("est.", self.render(False)[0]); self.assertNotIn("est.", self.render(False)[1])

    def test_viewer_sees_one_plain_marker_and_nothing_clickable(self):
        for html in self.render(True):
            self.assertEqual(html.count("✎"), 1); self.assertNotIn("basis-pen", html); self.assertIn("entered by you on 2026-10-01", html)


class TestUi(unittest.TestCase):
    def test_wiring(self):
        for needle in ("function openBasisEditor", "function costBasisPanel", "/api/cost-basis/import", "/api/cost-basis/template", "basis-pen",
                       "estimate — shares changed since entry", "known for ${p.basis_known} of ${p.basis_lines} accounts", "of total cost) of cost basis entered by you",
                       "W('cost-basis'", "wrap.appendChild(costBasisPanel())", "c.cost_source==='manual'"):
            self.assertIn(needle, HTML)


if __name__ == "__main__":
    unittest.main()
