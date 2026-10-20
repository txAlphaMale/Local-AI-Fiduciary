# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_snaptrade.py -- SnapTrade provider on SDK 13.x. Mapping is tested against responses RECORDED from
SnapTrade's real sandbox broker (test_data/snaptrade_sandbox.json); the client is faked for the mode logic."""
import copy, glob, json, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="snaptrade-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import snaptrade_integration as S  # noqa: E402

FX = json.load(open(os.path.join(_HERE, "test_data", "snaptrade_sandbox.json")))
PERS, COMM = "PERS-TESTCLIENT", "commercial-client"


class Resp:
    def __init__(self, body): self.body = body


class FakeAI:
    def __init__(self, fx, calls, fail=()):
        self.fx, self.calls, self.fail = fx, calls, set(fail)

    def list_all_user_accounts(self, **kw):
        self.calls.append(("list_all_user_accounts", kw)); return Resp({"results": self.fx["accounts"]})

    def get_all_account_positions(self, account_id=None, **kw):
        self.calls.append(("get_all_account_positions", dict(kw, account_id=account_id)))
        if account_id in self.fail:
            raise RuntimeError("boom")
        return Resp(self.fx["positions"][account_id])

    def get_user_account_balance(self, account_id=None, **kw):
        self.calls.append(("get_user_account_balance", dict(kw, account_id=account_id))); return Resp(self.fx["balances"][account_id])


class FakeConn:
    def __init__(self, fx, calls): self.fx, self.calls = fx, calls
    def list_brokerage_authorizations(self, **kw):
        self.calls.append(("list_brokerage_authorizations", kw)); return Resp(self.fx.get("connections", []))


class FakeAuth:
    def __init__(self, calls): self.calls = calls
    def login_snap_trade_user(self, **kw):
        self.calls.append(("login_snap_trade_user", kw)); return Resp({"redirectURI": "https://app.snaptrade.com/portal?x=1"})


class FakeClient:
    def __init__(self, fx=FX, calls=None, fail=()):
        self.calls = calls if calls is not None else []
        self.account_information, self.authentication, self.connections = FakeAI(fx, self.calls, fail), FakeAuth(self.calls), FakeConn(fx, self.calls)


def factory(client, mode="personal"):
    return lambda cid, ck, uid, us: (client, ({} if mode == "personal" else {"user_id": uid, "user_secret": us}), mode)


class TestMapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = S.map_portfolio(FX["accounts"], FX["positions"], FX["balances"], connections=FX["connections"])
        cls.by = {a["name"]: a for a in cls.m["accounts"]}

    def test_accounts_names_types_and_totals(self):
        self.assertEqual(sorted(self.by), ["IRA (...002)", "Individual (...001)"])
        self.assertEqual({a["institution"] for a in self.m["accounts"]}, {"sandbox"})     # resolved via the connections list
        self.assertTrue(any("IRA" in n for n in self.by))                       # raw type appended -> tax audit sees IRA
        self.assertEqual(sorted(a["balance"] for a in self.m["accounts"]), [36774.6, 68851.25])
        self.assertTrue(all(a["type"] == "Investments" and a["is_asset"] for a in self.m["accounts"]))
        self.assertEqual(self.m["net_worth"], round(68851.25 + 36774.60064, 2))

    def test_positions_sum_to_the_account_total_including_cash(self):
        for br in self.m["by_account"]:
            self.assertAlmostEqual(br["value"], br["balance"], places=1, msg=br["account"])

    def test_cash_equivalent_fund_is_not_double_counted(self):
        syms = [p["ticker"] for p in self.m["positions"]]
        self.assertNotIn("SPAXX", syms)                                          # already inside the IRA's cash
        self.assertIn("FXAIX", syms)
        cash = [p for p in self.m["positions"] if p["ticker"] == "CASH"]
        self.assertEqual(sorted(p["value"] for p in cash), [12500.0, 25000.0])

    def test_deprecated_account_schema_still_maps_the_same(self):
        legacy = S.map_portfolio(FX["legacy_accounts"], FX["positions"], FX["balances"])
        self.assertEqual(sorted(a["balance"] for a in legacy["accounts"]), sorted(a["balance"] for a in self.m["accounts"]))
        self.assertEqual({a["institution"] for a in legacy["accounts"]}, {"sandbox"})     # institution_name field
        self.assertEqual(len(legacy["positions"]), len(self.m["positions"]))

    def test_disabled_connection_is_called_out(self):
        conns = copy.deepcopy(FX["connections"]); conns[0]["disabled"] = True
        m = S.map_portfolio(FX["accounts"], FX["positions"], FX["balances"], connections=conns)
        self.assertTrue(any("disabled" in n for n in m["notes"]))

    def test_cost_basis_is_per_unit(self):
        aapl = next(p for p in self.m["positions"] if p["ticker"] == "AAPL")
        self.assertEqual((aapl["quantity"], aapl["price"], aapl["cost_total"], aapl["gain"]), (5.0, 180.5, 875.0, 27.5))
        btc = next(p for p in self.m["positions"] if p["ticker"] == "BTC")
        self.assertEqual((btc["type"], btc["cost_total"], btc["gain"]), ("Crypto", 15500.0, -750.0))

    def test_types(self):
        types = {p["ticker"]: p["type"] for p in self.m["positions"]}
        self.assertEqual((types["MSFT"], types["SPY"], types["FXAIX"], types["USDC"]), ("Stock", "ETF", "Mutual Fund", "Crypto"))

    def test_tax_status_from_name(self):
        import app as appmod
        for a in self.m["accounts"]:
            want = "deferred" if "IRA" in a["name"] else "taxable"
            self.assertEqual(appmod._account_tax_status(a["name"], a["type"]), want, a["name"])

    def test_options_are_noted_not_guessed(self):
        fx = copy.deepcopy(FX)
        aid = fx["accounts"][0]["id"]
        fx["positions"][aid]["results"].append({"units": "2", "price": "3.5", "cost_basis": "2", "currency": "USD",
                                                "instrument": {"kind": "option", "symbol": "AAPL251219C00200000", "description": "AAPL call"}})
        m = S.map_portfolio(fx["accounts"], fx["positions"], fx["balances"])
        self.assertNotIn("AAPL251219C00200000", [p["ticker"] for p in m["positions"]])
        self.assertTrue(any("option" in n for n in m["notes"]))

    def test_suspect_cost_basis_and_failed_account(self):
        fx = copy.deepcopy(FX)
        aid = fx["accounts"][0]["id"]
        fx["positions"][aid]["results"][0]["cost_basis"] = "99999999"
        m = S.map_portfolio(fx["accounts"], fx["positions"], fx["balances"], errors={fx["accounts"][1]["id"]: "boom"})
        p0 = next(p for p in m["positions"] if p["ticker"] == "AAPL")
        self.assertTrue(p0["suspect"]); self.assertIsNone(p0["cost_total"])
        self.assertTrue(any(b.get("error") == "boom" for b in m["by_account"]))

    def test_derive_accepts_the_mapping(self):
        import app as appmod
        m = self.m
        d = appmod._derive(m["accounts"], m["positions"], m["by_account"], m["inv"], m["net_worth"], m["total_assets"], 0.0, balance_only=m["balance_only"])
        self.assertEqual(d["account_count"], 2); self.assertAlmostEqual(d["invested_value"], m["net_worth"], places=0)


class TestModesAndBuild(unittest.TestCase):
    def test_mode_detection(self):
        self.assertEqual(S.auth_mode("PERS-ABC"), "personal"); self.assertEqual(S.auth_mode("abc"), "commercial")
        with mock.patch.dict(os.environ, {"SNAPTRADE_AUTH_MODE": "commercial"}):
            self.assertEqual(S.auth_mode("PERS-ABC"), "commercial")

    def test_make_client_uses_the_right_auth_helper_and_user_args(self):
        c, kw, mode = S.make_client(PERS, "ck")
        self.assertEqual((kw, mode), ({}, "personal")); self.assertEqual(type(c.configuration.auth).__name__ if hasattr(c, "configuration") else "PersonalApiKeyAuth", "PersonalApiKeyAuth")
        c, kw, mode = S.make_client(COMM, "ck", "u1", "s1")
        self.assertEqual((kw, mode), ({"user_id": "u1", "user_secret": "s1"}, "commercial"))

    def test_config_errors_are_clear(self):
        with self.assertRaisesRegex(S.SnapTradeConfigError, "SNAPTRADE_CLIENT_ID"):
            S.make_client(None, None)
        with self.assertRaisesRegex(S.SnapTradeConfigError, "SNAPTRADE_USER_ID"):
            S.make_client(COMM, "ck")

    def test_personal_mode_sends_no_user_args_commercial_sends_them(self):
        calls = []
        S.build_portfolio(PERS, "ck", client_factory=factory(FakeClient(calls=calls), "personal"))
        self.assertTrue(all(not ({"user_id", "user_secret"} & set(kw)) for _n, kw in calls))
        calls2 = []
        S.build_portfolio(COMM, "ck", "u1", "s1", client_factory=factory(FakeClient(calls=calls2), "commercial"))
        self.assertTrue(all(kw.get("user_id") == "u1" and kw.get("user_secret") == "s1" for _n, kw in calls2))

    def test_no_accounts_and_all_failed_raise_clear_errors(self):
        empty = dict(FX, accounts=[])
        with self.assertRaisesRegex(RuntimeError, "no accounts yet"):
            S.build_portfolio(PERS, "ck", client_factory=factory(FakeClient(empty)))
        ids = [a["id"] for a in FX["accounts"]]
        with self.assertRaisesRegex(RuntimeError, "every account failed"):
            S.build_portfolio(PERS, "ck", client_factory=factory(FakeClient(fail=ids)))

    def test_one_failing_account_does_not_sink_the_rest(self):
        m = S.build_portfolio(PERS, "ck", client_factory=factory(FakeClient(fail=[FX["accounts"][1]["id"]])))
        self.assertEqual(len(m["accounts"]), 2); self.assertTrue(any("error" in b for b in m["by_account"]))
        self.assertTrue(m["positions"])

    def test_portal_url_sandbox_and_normal(self):
        calls = []
        cl = FakeClient(calls=calls)
        self.assertTrue(S.portal_url(cl, {}, sandbox=True).startswith("https://app.snaptrade.com"))
        S.portal_url(cl, {"user_id": "u", "user_secret": "s"}, sandbox=False)
        sb, normal = calls[0][1], calls[1][1]
        self.assertEqual((sb.get("broker"), sb.get("connection_type")), ("SANDBOX", "read"))
        self.assertNotIn("broker", normal); self.assertEqual(normal["user_id"], "u")


class TestApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as appmod, audit, auth
        from fastapi.testclient import TestClient
        d = tempfile.mkdtemp(dir=_TMP)
        audit.AUDIT_DB = os.path.join(d, "audit.db"); audit.AUDIT_JSONL = os.path.join(d, "audit.jsonl"); audit.init_db()
        cls.appmod, cls.audit = appmod, audit
        for n, mk in (("st_admin", lambda n: auth.create_user(n, "correct-horse-battery-1", role="admin")), ("st_viewer", lambda n: auth.create_viewer(n, "correct-horse-battery-1"))):
            if not auth.get_user_by_username(n):
                mk(n)
        def login(u):
            c = TestClient(appmod.app); assert c.post("/api/auth/login", json={"username": u, "password": "correct-horse-battery-1"}).status_code == 200; return c
        cls.A, cls.V, cls.anon = login("st_admin"), login("st_viewer"), TestClient(appmod.app)

    def test_endpoints_are_admin_only(self):
        for method, path, body in (("get", "/api/integrations/snaptrade", None), ("post", "/api/integrations/snaptrade/portal", {})):
            for c, code in ((self.V, 403), (self.anon, 401)):
                r = getattr(c, method)(path, **({"json": body} if body is not None else {}))
                self.assertEqual(r.status_code, code, (path, code))

    def test_status_and_portal_for_a_personal_key(self):
        env = {"SNAPTRADE_CLIENT_ID": PERS, "SNAPTRADE_CONSUMER_KEY": "ck"}
        with mock.patch.dict(os.environ, env):
            st = self.A.get("/api/integrations/snaptrade").json()
            self.assertEqual((st["configured"], st["mode"]), (True, "personal"))
            calls = []
            with mock.patch.object(S, "make_client", lambda *a: (FakeClient(calls=calls), {}, "personal")):
                r = self.A.post("/api/integrations/snaptrade/portal", json={"sandbox": True})
            self.assertEqual(r.status_code, 200, r.text); self.assertTrue(r.json()["url"].startswith("https://"))
            self.assertEqual(calls[0][1].get("broker"), "SANDBOX")
        self.assertIn("integration.snaptrade.portal_link", {e["event_type"] for e in self.audit.query_core(limit=200)})
        self.assertNotIn("ck", json.dumps(self.audit.read_recent(limit=50), default=str).replace("check", ""))   # consumer key never audited

    def test_not_configured_gives_a_clean_400(self):
        with mock.patch.dict(os.environ, {"SNAPTRADE_CLIENT_ID": "", "SNAPTRADE_CONSUMER_KEY": ""}):
            r = self.A.post("/api/integrations/snaptrade/portal", json={})
        self.assertEqual(r.status_code, 400); self.assertIn("SNAPTRADE_CLIENT_ID", r.json()["error"])

    def test_build_through_the_app_uses_the_new_module(self):
        with mock.patch.dict(os.environ, {"SNAPTRADE_CLIENT_ID": PERS, "SNAPTRADE_CONSUMER_KEY": "ck", "PORTFOLIO_PROVIDER": "snaptrade"}), \
                mock.patch.object(S, "make_client", lambda *a: (FakeClient(), {}, "personal")):
            d = self.appmod.build()
        self.assertEqual((d["portfolio_provider"], d["snaptrade_mode"]), ("snaptrade", "personal"))
        self.assertEqual(d["account_count"], 2); self.assertAlmostEqual(d["net_worth"], 105625.85, places=1)


@unittest.skipUnless(os.environ.get("SNAPTRADE_LIVE_TEST") and os.environ.get("SNAPTRADE_CLIENT_ID"), "set SNAPTRADE_LIVE_TEST=1 + keys to hit SnapTrade's sandbox")
class TestLiveSandbox(unittest.TestCase):
    def test_live_sandbox_round_trip(self):
        m = S.build_portfolio(os.environ["SNAPTRADE_CLIENT_ID"], os.environ["SNAPTRADE_CONSUMER_KEY"],
                              os.environ.get("SNAPTRADE_USER_ID"), os.environ.get("SNAPTRADE_USER_SECRET"))
        self.assertTrue(m["accounts"] and m["positions"])


if __name__ == "__main__":
    unittest.main()
