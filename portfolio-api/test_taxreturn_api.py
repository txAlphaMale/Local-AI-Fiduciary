"""RV-T3: the endpoints. Admin-only upload/extract/review/confirm/delete; viewers see the card only when the admin switches it on and only after a
confirmation; a draft is never visible to the card, the tax engine or the chat; audit events carry no amounts."""
import asyncio, glob, io, json, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="tr-api-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import test_taxreturn as fx  # noqa: E402

try:
    import app
except Exception:
    app = None


class Req:
    def __init__(self, uid, body=None):
        self.session = {"uid": uid}
        self._b = body
        self.headers, self.client = {}, None
        self.state = type("S", (), {})()
    async def json(self):
        if self._b is None:
            raise ValueError("no body")
        return self._b


@unittest.skipIf(app is None, "app not importable")
class Api(unittest.TestCase):
    def setUp(self):
        self.pdf = fx.make_pdf(fx.layout(2024, fx.valid(2024), with_8959=250))
        self.events = []
        users = lambda uid: {"id": uid, "username": {"a": "owner", "v": "spouse"}.get(uid, uid), "role": "admin" if uid == "a" else "viewer"} if uid else None
        self.p = [mock.patch.object(app, "TAXRET_FILE", os.path.join(_TMP, "tr-%d.json" % id(self))),
                  mock.patch.object(app.audit, "emit", side_effect=lambda ev, **k: self.events.append((ev, k.get("detail")))),
                  mock.patch.object(app, "_emit_config_changed"),
                  mock.patch.object(app.auth, "get_user_by_id", side_effect=users),
                  mock.patch.object(app.auth, "is_admin", side_effect=lambda u: u.get("role") == "admin"),
                  mock.patch.object(app.vault, "get_item_row", side_effect=lambda i: {"id": i, "mime": "application/pdf", "deleted": 0} if i == "item1" else ({"id": i, "mime": "image/png", "deleted": 0} if i == "img" else None)),
                  mock.patch.object(app.vault, "read_plaintext", side_effect=lambda row: self.pdf)]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()

    def extract(self, uid="a", **b):
        return asyncio.run(app.post_taxreturn_extract(Req(uid, {"item_id": "item1", "year": 2024, **b})))

    def test_viewer_is_refused_everywhere_that_shows_or_changes_amounts(self):
        self.assertEqual(self.extract("v").status_code, 403)
        self.assertEqual(app.get_taxreturn(Req("v")).status_code, 403)
        self.assertEqual(asyncio.run(app.post_taxreturn_confirm("2024", Req("v", {"values": {}}))).status_code, 403)
        self.assertEqual(app.delete_taxreturn("2024", Req("v")).status_code, 403)
        self.assertEqual(asyncio.run(app.post_taxreturn_config(Req("v", {"viewers_see": True}))).status_code, 403)

    def test_extract_makes_a_draft_and_nothing_is_visible_until_confirmed(self):
        r = self.extract()
        self.assertTrue(r["ok"])
        f = {x["field"]: x for x in r["review"]["fields"]}
        self.assertEqual((f["agi"]["value"], f["additional_medicare_tax"]["value"]), (126700.0, 250.0))
        self.assertIn("2024", app.get_taxreturn(Req("a"))["drafts"])
        self.assertIsNone(app.get_taxreturn_card(Req("a"))["card"])                   # a draft never reaches the card
        self.assertFalse(app.get_taxreturn(Req("a"))["confirmed"])

    def test_confirm_stores_values_records_who_and_when_and_what_was_edited(self):
        self.extract()
        vals = {x["field"]: x["value"] for x in app.get_taxreturn(Req("a"))["drafts"]["2024"]["review"]["fields"]}
        vals["taxable_interest"] = 1300.0                                                # the admin corrects one value
        ok = asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": vals})))
        self.assertTrue(ok["ok"])
        d = app.get_taxreturn(Req("a"))
        self.assertNotIn("2024", d["drafts"])
        c = d["confirmed"]["2024"]
        self.assertEqual((c["confirmed_by"], c["edited_fields"]), ("owner", ["taxable_interest"]))
        self.assertGreater(c["confirmed_at"], 0)
        self.assertEqual(c["summary"]["agi"], 126700.0)
        card = app.get_taxreturn_card(Req("a"))["card"]
        self.assertEqual((card["year"], card["summary"]["total_tax"], card["summary"]["effective_rate_pct"]), (2024, 11000.0, 8.7))
        self.assertEqual(card["starting_points"], {"ordinary_income": 118700.0, "long_term_gains": 8000.0})

    def test_a_viewer_sees_the_card_only_after_the_admin_switches_it_on(self):
        self.extract()
        vals = {x["field"]: x["value"] for x in app.get_taxreturn(Req("a"))["drafts"]["2024"]["review"]["fields"]}
        asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": vals})))
        self.assertEqual(app.get_taxreturn_card(Req("v")), {"card": None, "enabled": False})       # default off
        asyncio.run(app.post_taxreturn_config(Req("a", {"viewers_see": True})))
        self.assertEqual(app.get_taxreturn_card(Req("v"))["card"]["year"], 2024)
        self.assertEqual(app.get_taxreturn_card(Req(None)).status_code, 401)

    def test_unreliable_values_that_are_kept_are_recorded(self):
        pdf_vals = fx.valid(2024); pdf_vals["agi"] = 127700
        self.pdf = fx.make_pdf(fx.layout(2024, pdf_vals))
        r = self.extract()
        vals = {x["field"]: x["value"] for x in r["review"]["fields"]}
        asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": vals})))
        self.assertIn("agi", app.get_taxreturn(Req("a"))["confirmed"]["2024"]["unreliable_confirmed"])

    def test_refusals_are_plain(self):
        pdf2022 = fx.layout(2024, fx.valid(2024))
        self.pdf = fx.make_pdf([[(x, y, t.replace("2024", "2022")) for x, y, t in pg] for pg in pdf2022])
        r = self.extract(year=2022)
        self.assertEqual(r.status_code, 400)
        self.assertIn("No line map for tax year 2022", json.loads(r.body)["error"])
        self.pdf = fx.make_pdf(fx.layout(2024, fx.valid(2024)))
        self.assertEqual(asyncio.run(app.post_taxreturn_extract(Req("a", {"item_id": "nope", "year": 2024}))).status_code, 404)
        self.assertEqual(asyncio.run(app.post_taxreturn_extract(Req("a", {"item_id": "img", "year": 2024}))).status_code, 400)
        self.assertEqual(asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": {}}))).status_code, 404)       # nothing extracted yet
        self.extract()
        self.assertEqual(asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": {"ssn": 1}}))).status_code, 400)
        self.assertEqual(asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": {"agi": "lots"}}))).status_code, 400)

    def test_delete_removes_the_record_and_the_card(self):
        self.extract()
        vals = {x["field"]: x["value"] for x in app.get_taxreturn(Req("a"))["drafts"]["2024"]["review"]["fields"]}
        asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": vals})))
        self.assertTrue(app.delete_taxreturn("2024", Req("a"))["ok"])
        self.assertIsNone(app.get_taxreturn_card(Req("a"))["card"])
        self.assertEqual(app.delete_taxreturn("2024", Req("a")).status_code, 404)

    def test_audit_events_carry_no_amounts_and_the_store_has_no_nine_digit_run(self):
        self.extract()
        vals = {x["field"]: x["value"] for x in app.get_taxreturn(Req("a"))["drafts"]["2024"]["review"]["fields"]}
        asyncio.run(app.post_taxreturn_confirm("2024", Req("a", {"values": vals})))
        app.delete_taxreturn("2024", Req("a"))
        names = [e for e, _d in self.events]
        self.assertEqual(names, ["taxreturn.extracted", "taxreturn.confirmed", "taxreturn.deleted"])
        for _e, detail in self.events:
            blob = json.dumps(detail)
            for amount in ("126700", "126,700", "97500", "11000", "120000"):
                self.assertNotIn(amount, blob)
        self.extract()
        raw = json.dumps(app._tr_read())
        self.assertIsNone(re.search(r"(?<![\d.])\d{9}(?![\d.])|\b\d{3}-\d{2}-\d{4}\b", raw))        # a standalone 9-digit number or an SSN pattern
        self.assertNotIn("Jason", raw)

    def test_nothing_outside_the_filed_return_handlers_reads_the_store(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        start, end = src.index("# ---- Filed returns (RV-T3"), src.index("_AL_BUCKET = {")
        outside = src[:start] + src[end:]
        self.assertNotIn("TAXRET_FILE", outside)
        self.assertNotIn("_tr_read()", outside)
        for fn in ("def get_tax(", "def _tax_ctx(", "def _fiduciary_dispatch("):
            i = src.index(fn)
            nxt = [j for j in (src.find("\ndef ", i + 5), src.find("\n@app.", i + 5)) if j > 0]
            body = src[i:min(nxt)] if nxt else src[i:]
            self.assertNotIn("taxreturn", body.lower(), fn)                                # no engine, panel or chat tool reads a filed return


if __name__ == "__main__":
    unittest.main()
