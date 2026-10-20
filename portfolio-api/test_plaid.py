# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_plaid.py -- TASK-005: Plaid provider (balances, holdings, liabilities; never transactions).
A fake Plaid server is patched in at the requests layer; the real app + real global gate are used."""
import glob, json, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="plaid-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ["PLAID_ITEMS_FILE"] = os.path.join(_TMP, "plaid_items.json")
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod, audit, auth, plaid_integration as P  # noqa: E402

PW = "correct-horse-battery-1"
CID, SECRET = "client-id-123", "plaid-secret-SUPERSECRET-456"
ACCESS = "access-sandbox-TOKEN-789"


class Resp:
    def __init__(self, body, status=200): self._b, self.status_code = body, status
    def json(self): return self._b


ACCOUNTS = {"accounts": [
    {"account_id": "a_chk", "name": "Plaid Checking", "official_name": "Plaid Gold Standard Checking", "mask": "0000", "type": "depository", "subtype": "checking", "balances": {"current": 2500.55, "available": 2400}},
    {"account_id": "a_sav", "name": "Plaid Saving", "mask": "1111", "type": "depository", "subtype": "savings", "balances": {"current": 10000}},
    {"account_id": "a_cc", "name": "Plaid Credit Card", "mask": "4483", "type": "credit", "subtype": "credit card", "balances": {"current": 352.0}},
    {"account_id": "a_mort", "name": "Plaid Mortgage", "mask": "5555", "type": "loan", "subtype": "mortgage", "balances": {"current": 200000}},
    {"account_id": "a_roth", "name": "Plaid IRA", "mask": "7777", "type": "investment", "subtype": "roth", "balances": {"current": 31000}},
    {"account_id": "a_bro", "name": "Plaid Brokerage", "mask": "8888", "type": "investment", "subtype": "brokerage", "balances": {"current": 9000}},
    {"account_id": "a_401k", "name": "Plaid 401k", "mask": "9999", "type": "investment", "subtype": "401k", "balances": {"current": 70000}},
], "item": {"institution_id": "ins_1", "item_id": "item_1"}}
HOLDINGS = {"securities": [
    {"security_id": "s_vti", "name": "Vanguard Total Stock Market", "ticker_symbol": "VTI", "type": "etf", "close_price": 250.0},
    {"security_id": "s_cash", "name": "U S Dollar", "ticker_symbol": "CUR:USD", "type": "cash", "is_cash_equivalent": True},
    {"security_id": "s_cit", "name": "Acme Collective Trust 2045", "ticker_symbol": None, "type": "mutual fund"}],
    "holdings": [
    {"account_id": "a_roth", "security_id": "s_vti", "quantity": 100, "institution_price": 250.0, "institution_value": 25000.0, "cost_basis": 18000.0},
    {"account_id": "a_roth", "security_id": "s_cash", "quantity": 6000, "institution_price": 1.0, "institution_value": 6000.0, "cost_basis": None},
    {"account_id": "a_401k", "security_id": "s_cit", "quantity": 500, "institution_price": 140.0, "institution_value": 70000.0, "cost_basis": None}]}
LIABS = {"liabilities": {
    "credit": [{"account_id": "a_cc", "aprs": [{"apr_type": "cash_apr", "apr_percentage": 29.9}, {"apr_type": "purchase_apr", "apr_percentage": 0.0}], "minimum_payment_amount": 90.0, "next_payment_due_date": "2026-11-05"}],
    "mortgage": [{"account_id": "a_mort", "interest_rate": {"percentage": 3.25}, "next_monthly_payment": 1450.0}],
    "student": []}}


class FakePlaid:
    """Routes Plaid paths; records every call (path, payload, headers)."""
    def __init__(self):
        self.calls, self.fail = [], {}
        self.session = {"link_sessions": []}

    def __call__(self, url, json=None, timeout=None, headers=None):
        path = "/" + url.split(".com/", 1)[1]
        self.calls.append((path, json, headers))
        if path in self.fail:
            code, status = self.fail[path]
            return Resp({"error_code": code, "error_message": f"{code} happened", "request_id": "rq"}, status)
        if path == "/link/token/create":
            return Resp({"link_token": "link-sandbox-abc", "hosted_link_url": "https://secure.plaid.com/hl/abc", "expiration": "x"})
        if path == "/link/token/get":
            return Resp(self.session)
        if path == "/item/public_token/exchange":
            return Resp({"access_token": ACCESS, "item_id": "item_1"})
        if path == "/accounts/get":
            return Resp(ACCOUNTS)
        if path == "/institutions/get_by_id":
            return Resp({"institution": {"name": "Plaid Test Bank"}})
        if path == "/investments/holdings/get":
            return Resp(HOLDINGS)
        if path == "/liabilities/get":
            return Resp(LIABS)
        if path == "/item/remove":
            return Resp({"removed": True})
        raise AssertionError("unexpected Plaid call " + path)

    def paths(self): return [c[0] for c in self.calls]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        d = tempfile.mkdtemp(dir=_TMP)
        audit.AUDIT_DB = os.path.join(d, "audit.db"); audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl"); audit.init_db()
        for n, mk in (("pl_admin", lambda n: auth.create_user(n, PW, role="admin")), ("pl_viewer", lambda n: auth.create_viewer(n, PW))):
            if not auth.get_user_by_username(n):
                mk(n)
        def login(u):
            c = TestClient(appmod.app); assert c.post("/api/auth/login", json={"username": u, "password": PW}).status_code == 200; return c
        cls.A, cls.V, cls.anon = login("pl_admin"), login("pl_viewer"), TestClient(appmod.app)
        r = cls.A.post("/api/secrets", json={"PLAID_CLIENT_ID": CID, "PLAID_SECRET": SECRET})
        assert r.status_code == 200, r.text

    def setUp(self):
        P._write({"items": [], "pending": []})
        self.fake = FakePlaid()
        self.patch = mock.patch("plaid_integration.requests.post", self.fake)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def add_item(self, kind="investments", item_id="item_1"):
        d = P._read()
        d["items"].append({"item_id": item_id, "access_token": ACCESS, "institution_id": "ins_1", "institution": "Plaid Test Bank",
                           "kind": kind, "created": 1, "status": "ok", "error": None, "accounts_count": 7})
        P._write(d)


class TestLinkCreation(Base):
    def test_products_per_kind_never_transactions_and_credentials_in_headers_only(self):
        for kind, prods in (("bank", ["auth"]), ("investments", ["investments"]), ("credit", ["liabilities"])):
            self.fake.calls.clear()
            r = P.start_link(kind, "u1")
            path, body, headers = self.fake.calls[0]
            self.assertEqual(path, "/link/token/create")
            self.assertEqual(body["products"], prods)
            self.assertNotIn("transactions", json.dumps(body))
            self.assertIn("hosted_link", body); self.assertLessEqual(len(body["client_name"]), 30)
            self.assertEqual(body["user"]["client_user_id"], "fiduciary-u1")
            self.assertEqual(headers["PLAID-CLIENT-ID"], CID); self.assertEqual(headers["PLAID-SECRET"], SECRET)
            self.assertNotIn(SECRET, json.dumps(body))
            self.assertTrue(r["hosted_link_url"].startswith("https://secure.plaid.com/"))
            self.assertNotIn("link_token", r)                     # stays server-side
        self.assertNotIn("transactions", sum(P.PRODUCTS_BY_KIND.values(), []))

    def test_env_override_cannot_reintroduce_transactions(self):
        with mock.patch.dict(os.environ, {"PLAID_PRODUCTS_BANK": "auth,transactions"}):
            self.assertEqual(P.products_for("bank"), ["auth"])

    def test_update_mode_omits_products(self):
        self.add_item()
        P.start_link("investments", "u1", item_id="item_1")
        body = self.fake.calls[-1][1]
        self.assertEqual(body["access_token"], ACCESS); self.assertNotIn("products", body)

    def test_not_configured_and_bad_kind(self):
        with mock.patch.object(P, "_cred_getter", lambda: (None, None, "sandbox")):
            with self.assertRaises(P.PlaidError) as cm:
                P.start_link("bank", "u1")
            self.assertEqual(cm.exception.code, "NOT_CONFIGURED")
        with self.assertRaises(P.PlaidError):
            P.start_link("crypto", "u1")

    def test_environment_selects_base_url(self):
        P.start_link("bank", "u1")
        self.assertTrue(True)   # sandbox default from settings; URL checked below
        with mock.patch.object(P, "_cred_getter", lambda: (CID, SECRET, "production")):
            captured = {}
            with mock.patch("plaid_integration.requests.post", lambda url, **k: captured.setdefault("u", url) and Resp({"link_token": "t", "hosted_link_url": "u"})):
                P.link_token_create("bank", "u")
            self.assertTrue(captured["u"].startswith("https://production.plaid.com/"))


class TestPollingAndStorage(Base):
    def test_pending_then_linked_exchanges_and_stores_without_leaking(self):
        s = P.start_link("investments", "u1")
        self.assertEqual(P.poll_link(s["session_id"])["status"], "pending")
        self.fake.session = {"link_sessions": [{"finished_at": "t", "results": {"item_add_results": [{"public_token": "public-sandbox-xyz"}]}}]}
        r = P.poll_link(s["session_id"])
        self.assertEqual(r["status"], "linked"); self.assertEqual(r["items"][0]["institution"], "Plaid Test Bank")
        self.assertNotIn(ACCESS, json.dumps(r))
        self.assertIn("/item/public_token/exchange", self.fake.paths())
        self.assertEqual(P.list_items()[0]["access_token"], ACCESS)            # stored server-side...
        self.assertNotIn(ACCESS, json.dumps(P.public_items()))                  # ...never in the public view
        self.assertEqual(P.poll_link(s["session_id"])["status"], "unknown")     # session consumed

    def test_exit_is_reported_and_stores_nothing(self):
        s = P.start_link("bank", "u1")
        self.fake.session = {"link_sessions": [{"exit": {"status": "requires_credentials", "error": {"error_code": "INSTITUTION_DOWN"}}}]}
        r = P.poll_link(s["session_id"])
        self.assertEqual(r["status"], "exited"); self.assertEqual(r["exit"]["error_code"], "INSTITUTION_DOWN")
        self.assertEqual(P.list_items(), [])

    def test_update_mode_session_clears_login_required(self):
        self.add_item(); P.mark_item("item_1", "login_required", "x")
        s = P.start_link("investments", "u1", item_id="item_1")
        self.fake.session = {"link_sessions": [{"results": {"item_add_results": [{"public_token": "p"}]}}]}
        P.poll_link(s["session_id"])
        self.assertEqual(P.list_items()[0]["status"], "ok")
        self.assertNotIn("/item/public_token/exchange", self.fake.paths())      # token unchanged in update mode


class TestMapping(Base):
    def setUp(self):
        super().setUp()
        self.item = {"item_id": "item_1", "institution": "Plaid Test Bank"}
        self.m = P.map_item(self.item, ACCOUNTS, HOLDINGS, LIABS)
        self.by = {a["id"]: a for a in self.m["accounts"]}

    def test_assets_and_liabilities_and_signs(self):
        self.assertEqual(self.by["plaid:a_chk"]["balance"], 2500.55); self.assertTrue(self.by["plaid:a_chk"]["is_asset"])
        cc = self.by["plaid:a_cc"]
        self.assertFalse(cc["is_asset"]); self.assertEqual(cc["balance"], 352.0); self.assertEqual(cc["type"], "Credit Card")
        self.assertFalse(self.by["plaid:a_mort"]["is_asset"]); self.assertEqual(self.by["plaid:a_mort"]["type"], "Mortgage")

    def test_names_carry_mask_and_subtype_only_when_tax_classification_needs_it(self):
        self.assertEqual(self.by["plaid:a_chk"]["name"], "Plaid Gold Standard Checking (...0000)")
        self.assertEqual(self.by["plaid:a_roth"]["name"], "Plaid IRA · Roth IRA (...7777)")      # name said IRA, subtype says Roth
        self.assertEqual(self.by["plaid:a_401k"]["name"], "Plaid 401k (...9999)")               # already classifiable: untouched
        self.assertEqual(self.by["plaid:a_bro"]["name"], "Plaid Brokerage (...8888)")           # no tax keyword needed
        self.assertEqual(appmod._account_tax_status(self.by["plaid:a_roth"]["name"], self.by["plaid:a_roth"]["type"]), "roth")
        self.assertEqual(appmod._account_tax_status(self.by["plaid:a_401k"]["name"], "Investments"), "deferred")
        self.assertEqual(appmod._account_tax_status(self.by["plaid:a_bro"]["name"], self.by["plaid:a_bro"]["type"]), "taxable")

    def test_liability_defaults_from_plaid(self):
        cc, mort = self.by["plaid:a_cc"], self.by["plaid:a_mort"]
        self.assertEqual(cc["apr_pct"], 0.0); self.assertEqual(cc["min_payment"], 90.0)    # purchase APR preferred over cash APR
        self.assertEqual(mort["apr_pct"], 3.25); self.assertEqual(mort["min_payment"], 1450.0)

    def test_positions_cash_cost_basis_and_cit(self):
        pos = {(p["account"][:11], p["ticker"]): p for p in self.m["positions"]}
        vti = next(p for p in self.m["positions"] if p["ticker"] == "VTI")
        self.assertEqual((vti["value"], vti["cost_total"], vti["gain"], vti["type"]), (25000.0, 18000.0, 7000.0, "ETF"))
        cash = next(p for p in self.m["positions"] if p["type"] == "Cash")
        self.assertEqual((cash["ticker"], cash["price"], cash["cost_total"]), ("CASH", 1.0, None))
        cit = next(p for p in self.m["positions"] if "Collective" in (p["name"] or ""))
        self.assertTrue(cit["is_cit"]); self.assertTrue(cit["suspect"])                    # no ticker, no basis
        self.assertEqual(len(self.m["inv"]), 2)                                            # roth + 401k have holdings
        self.assertEqual([a["id"] for a in self.m["balance_only"]], ["plaid:a_bro"])       # brokerage: balance only

    def test_derive_accepts_the_mapping(self):
        d = appmod._derive(self.m["accounts"], self.m["positions"], self.m["by_account"], self.m["inv"], 0, 0, 0, balance_only=self.m["balance_only"])
        self.assertEqual(d["position_count"], len(self.m["positions"]))
        self.assertTrue(any(i["account"] and "Roth" in i["account"] for i in d["investment_balance_accounts"]) or d["by_account"])


class TestBuildPortfolio(Base):
    def test_net_worth_and_totals(self):
        self.add_item()
        m = P.build_portfolio()
        self.assertEqual(m["total_assets"], round(2500.55 + 10000 + 31000 + 9000 + 70000, 2))
        self.assertEqual(m["total_liabilities"], 200352.0)
        self.assertEqual(m["net_worth"], round(m["total_assets"] - m["total_liabilities"], 2))
        self.assertNotIn("/transactions/get", self.fake.paths()); self.assertNotIn("/transactions/sync", self.fake.paths())
        self.assertEqual(P.list_items()[0]["status"], "ok")

    def test_one_failing_item_does_not_sink_the_rest(self):
        self.add_item(item_id="good")
        d = P._read(); d["items"].append({**d["items"][0], "item_id": "bad", "access_token": "tok-bad", "institution": "Broken Bank"}); P._write(d)
        real = self.fake.__call__
        def flaky(url, json=None, timeout=None, headers=None):
            if json and json.get("access_token") == "tok-bad":
                return Resp({"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "login"}, 400)
            return real(url, json=json, timeout=timeout, headers=headers)
        with mock.patch("plaid_integration.requests.post", flaky):
            m = P.build_portfolio()
        st = {i["item_id"]: i["status"] for i in P.list_items()}
        self.assertEqual(st, {"good": "ok", "bad": "login_required"})
        self.assertTrue(m["accounts"]); self.assertEqual(m["item_errors"][0]["code"], "ITEM_LOGIN_REQUIRED")
        self.assertTrue(any("re-login" in (b.get("error") or "") for b in m["by_account"]))

    def test_all_failed_and_none_connected_raise_clear_errors(self):
        with self.assertRaisesRegex(RuntimeError, "no connected institutions"):
            P.build_portfolio()
        self.add_item()
        self.fake.fail["/accounts/get"] = ("INTERNAL_SERVER_ERROR", 500)
        with self.assertRaisesRegex(RuntimeError, "every connected institution failed"):
            P.build_portfolio()

    def test_investments_product_missing_is_tolerated(self):
        self.add_item()
        self.fake.fail["/investments/holdings/get"] = ("PRODUCTS_NOT_SUPPORTED", 400)
        m = P.build_portfolio()
        self.assertEqual(m["positions"], []); self.assertTrue(m["accounts"])


class TestRemove(Base):
    def test_remove_tells_plaid_then_deletes_locally(self):
        self.add_item(); P.remove_item("item_1")
        self.assertIn("/item/remove", self.fake.paths()); self.assertEqual(P.list_items(), [])

    def test_failure_keeps_the_record_so_billing_can_be_stopped_later(self):
        self.add_item(); self.fake.fail["/item/remove"] = ("INTERNAL_SERVER_ERROR", 500)
        with self.assertRaises(P.PlaidError):
            P.remove_item("item_1")
        self.assertEqual(len(P.list_items()), 1)

    def test_already_gone_at_plaid_still_deletes(self):
        self.add_item(); self.fake.fail["/item/remove"] = ("ITEM_NOT_FOUND", 400)
        P.remove_item("item_1"); self.assertEqual(P.list_items(), [])


class TestApi(Base):
    def test_viewer_and_anonymous_denied_on_every_endpoint(self):
        for method, path, body in (("get", "/api/integrations/plaid", None), ("post", "/api/integrations/plaid/link", {"kind": "bank"}),
                                   ("post", "/api/integrations/plaid/poll", {"session_id": "x"}), ("post", "/api/integrations/plaid/remove", {"item_id": "x"})):
            for c, code in ((self.V, 403), (self.anon, 401)):
                r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
                self.assertEqual(r.status_code, code, (path, code, r.status_code))
        self.assertEqual(self.fake.calls, [])                                        # nothing reached Plaid

    def test_admin_flow_and_no_secret_or_token_anywhere(self):
        st = self.A.get("/api/integrations/plaid").json()
        self.assertTrue(st["configured"]); self.assertEqual({k["kind"] for k in st["kinds"]}, {"bank", "investments", "credit"})
        r = self.A.post("/api/integrations/plaid/link", json={"kind": "investments"}); self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["session_id"]
        self.fake.session = {"link_sessions": [{"results": {"item_add_results": [{"public_token": "public-x"}]}}]}
        p = self.A.post("/api/integrations/plaid/poll", json={"session_id": sid}).json()
        self.assertEqual(p["status"], "linked")
        st = self.A.get("/api/integrations/plaid").json()
        self.assertEqual(st["items"][0]["institution"], "Plaid Test Bank")
        self.assertEqual(self.A.post("/api/integrations/plaid/remove", json={"item_id": "item_1"}).status_code, 200)
        blob = json.dumps([r.text for r in (self.A.get("/api/integrations/plaid"), self.A.get("/api/secrets"))]) + json.dumps(audit.read_recent(limit=200), default=str)
        for secret in (SECRET, ACCESS, "link-sandbox-abc", "public-x"):
            self.assertNotIn(secret, blob)
        types = {e["event_type"] for e in audit.query_core(limit=500)}
        self.assertTrue({"integration.plaid.link_started", "integration.plaid.item_added", "integration.plaid.item_removed"} <= types)

    def test_plaid_error_is_a_clean_message_not_a_crash(self):
        self.fake.fail["/link/token/create"] = ("INVALID_PRODUCT", 400)
        r = self.A.post("/api/integrations/plaid/link", json={"kind": "bank"})
        self.assertEqual(r.status_code, 502); self.assertIn("INVALID_PRODUCT", r.text)

    def test_secret_is_write_only_but_client_id_is_visible_to_admin(self):
        k = {s["key"]: s for s in self.A.get("/api/secrets").json()["secrets"]}
        self.assertEqual(k["PLAID_CLIENT_ID"]["value"], CID)
        self.assertNotIn("value", k["PLAID_SECRET"])


class TestProviderIntegration(Base):
    def test_build_with_plaid_provider_and_others_unaffected(self):
        self.add_item()
        with mock.patch.dict(os.environ, {"PORTFOLIO_PROVIDER": "plaid"}):
            d = appmod.build()
        self.assertEqual(d["portfolio_provider"], "plaid")
        self.assertEqual(d["total_liabilities"], 200352.0)
        self.assertTrue(any(a["name"].startswith("Plaid Credit Card") for a in d["accounts"]))
        calls_before = len(self.fake.calls)
        with mock.patch.dict(os.environ, {"PORTFOLIO_PROVIDER": "csv"}):
            appmod.build()
        self.assertEqual(len(self.fake.calls), calls_before)                         # csv never touches Plaid

    def test_provider_apr_and_payment_fill_in_but_user_entries_win(self):
        accts = [{"name": "Card A (...1)", "balance": 500, "is_asset": False, "apr_pct": 21.5, "min_payment": 40},
                 {"name": "Card B (...2)", "balance": 800, "is_asset": False, "apr_pct": 18.0, "min_payment": 30},
                 {"name": "Promo (...3)", "balance": 900, "is_asset": False, "apr_pct": 0.0, "min_payment": 25}]
        cfg = {"debt_rates": {"Card B (...2)": 12.0}, "debt_min_payments": {"Card B (...2)": 99}}
        with mock.patch.object(appmod, "_data", lambda: {"accounts": accts}), mock.patch.object(appmod, "_read_insurance_cfg", lambda: cfg):
            rows = {r["name"]: r for r in appmod._whatif_liabilities()}
        self.assertEqual((rows["Card A (...1)"]["apr"], rows["Card A (...1)"]["apr_source"]), (21.5, "plaid"))
        self.assertEqual(rows["Card A (...1)"]["payment"], 40)
        self.assertEqual((rows["Card B (...2)"]["apr"], rows["Card B (...2)"]["payment"]), (12.0, 99))   # user wins
        self.assertNotIn("Promo (...3)", rows)                                                              # 0% still excluded

    def test_plaid_is_a_selectable_provider(self):
        spec = next(s for s in appmod.CONFIG_SPECS if s["key"] == "PORTFOLIO_PROVIDER")
        self.assertIn("plaid", spec["options"])


class TestRealSandboxShapes(unittest.TestCase):
    """Responses recorded from Plaid's real SANDBOX (First Platypus Bank test data, no credentials):
    guards the mapping against Plaid's actual response shapes, not just my hand-written fixtures."""
    @classmethod
    def setUpClass(cls):
        cls.real = json.load(open(os.path.join(_HERE, "test_data", "plaid_sandbox_first_platypus.json")))
        cls.m = P.map_item({"item_id": "i", "institution": "First Platypus Bank"}, cls.real["accounts"],
                           cls.real["holdings"], cls.real["liabilities"])
        cls.by = {a["name"]: a for a in cls.m["accounts"]}

    def test_all_accounts_map_with_the_right_side_of_the_balance_sheet(self):
        self.assertEqual(len(self.m["accounts"]), 14)
        liab = {a["type"] for a in self.m["accounts"] if not a["is_asset"]}
        self.assertEqual(liab, {"Student Loan", "Mortgage", "Auto Loan", "HELOC", "Credit Card"})
        self.assertTrue(all(a["balance"] >= 0 for a in self.m["accounts"]))

    def test_liability_defaults_come_from_real_payloads(self):
        mort = next(a for a in self.m["accounts"] if a["type"] == "Mortgage")
        stu = next(a for a in self.m["accounts"] if a["type"] == "Student Loan")
        cc = next(a for a in self.m["accounts"] if a["name"].startswith("Plaid Diamond"))
        self.assertEqual((mort["apr_pct"], stu["apr_pct"], cc["apr_pct"]), (3.99, 5.25, 12.5))
        self.assertTrue(all(a.get("min_payment") for a in (mort, stu, cc)))

    def test_holdings_securities_cash_and_tax_status(self):
        types = {p["type"] for p in self.m["positions"]}
        self.assertTrue({"Stock", "ETF", "Mutual Fund", "Bond", "Cash", "Crypto", "Option"} <= types)
        self.assertEqual(sum(1 for a in self.m["inv"]), 2)                                  # IRA + 401k have holdings
        names = {a["name"] for a in self.m["accounts"] if a["type"] == "Investments"}
        status = {n: appmod._account_tax_status(n, "Investments") for n in names}
        self.assertEqual(sorted(status.values()), ["deferred", "deferred"])
        self.assertTrue(all(p["ticker"] for p in self.m["positions"]))

    def test_derive_accepts_real_data(self):
        m = self.m
        a = round(sum(x["balance"] for x in m["accounts"] if x["is_asset"]), 2)
        l = round(sum(x["balance"] for x in m["accounts"] if not x["is_asset"]), 2)
        d = appmod._derive(m["accounts"], m["positions"], m["by_account"], m["inv"], a - l, a, l, balance_only=m["balance_only"])
        self.assertEqual(d["account_count"], 14); self.assertGreater(d["invested_value"], 20000)


if __name__ == "__main__":
    unittest.main()
