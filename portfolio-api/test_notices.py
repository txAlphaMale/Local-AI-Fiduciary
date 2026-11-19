"""RV-T1 / TASK-RV-044: the standard notices are in the places the task lists, say the same thing everywhere, and the demo shows sample data only."""
import glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="notices-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import notices, notifications, thesis  # noqa: E402

HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
README = open(os.path.join(_HERE, "..", "README.md"), encoding="utf-8").read()


class Wording(unittest.TestCase):
    def test_the_page_repeats_the_same_wording_as_notices_py(self):
        for const, val in (("N_PAPER", notices.PAPER), ("N_ESTIMATE", notices.ESTIMATE), ("N_LEAD", notices.LEAD), ("N_NAME", notices.NAME),
                           ("N_ACK_TITLE", notices.ACK_TITLE), ("N_ACK_TEXT", notices.ACK_TEXT)):
            self.assertIn(f"{const}='{val}'", HTML, const)
        self.assertIn("const TL_CLOSING='" + notices.CLOSING + "';", HTML)
        self.assertIn("N_CLOSING=TL_CLOSING", HTML)

    def test_with_notice_keeps_the_line_and_shortens_the_body(self):
        out = notices.with_notice("x" * 1000, 300)
        self.assertTrue(out.endswith(notices.CLOSING))
        self.assertLessEqual(len(out), 300)
        self.assertEqual(notices.with_notice("short", 300), "short\n\n" + notices.CLOSING)


class Surface1TextThatLeaves(unittest.TestCase):
    def test_push_and_email_bodies_end_with_the_line(self):
        sent = {}
        with mock.patch.object(notifications, "_read_subs", return_value={"u": [{"endpoint": "e"}]}), \
             mock.patch.object(notifications, "_read_prefs", return_value={"u": {"push": True, "email": True, "categories": {}}}), \
             mock.patch.object(notifications, "_send_push", side_effect=lambda sub, t, b: sent.setdefault("push", b) or True), \
             mock.patch.object(notifications, "_send_email", side_effect=lambda t, b: sent.setdefault("email", b) or True), \
             mock.patch.object(notifications, "smtp_configured", return_value=True), \
             mock.patch.object(notifications, "_admin_uid", return_value="u"):
            notifications._dispatch_message("reminders", "T", "Your report is ready")
        self.assertTrue(sent["push"].endswith(notices.CLOSING))
        self.assertTrue(sent["email"].endswith(notices.CLOSING))

    def test_the_test_notification_ends_with_the_line_on_both_channels(self):
        got = []
        with mock.patch.object(notifications, "get_prefs", return_value={"push": True, "email": True}), \
             mock.patch.object(notifications, "_read_subs", return_value={"u": [{"e": 1}]}), \
             mock.patch.object(notifications, "_send_push", side_effect=lambda s, t, b: got.append(b) or True), \
             mock.patch.object(notifications, "_send_email", side_effect=lambda t, b: got.append(b) or True), \
             mock.patch.object(notifications, "smtp_configured", return_value=True):
            notifications.send_test("u", True)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(b.endswith(notices.CLOSING) for b in got))

    def test_the_cash_check_copy_ends_with_the_line(self):
        i = HTML.index("function cashCheckText(masked){")
        body = HTML[i:HTML.index("\n}\n", i)]
        self.assertIn("L.push('',N_CLOSING);", body)


class Surface2Describe(unittest.TestCase):
    def test_a_tripped_thesis_describes_it_does_not_say_exit(self):
        r = thesis.evaluate_thesis({"ticker": "X", "triggers": [{"type": "metric", "metric": "m", "op": "<", "threshold": 5}]}, {"m": 1})
        self.assertNotRegex(r["action"], r"(?i)exit|sell|trim|buy")
        self.assertIn("Thesis check", r["action"])

    def test_no_attention_item_wording_tells_you_to_trade(self):
        src = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
        body = src[src.index("def _alerts_digest"):src.index("def get_alerts")]
        for m in re.finditer(r'add\("(?:action|warn)",[^\n]*\n(?:[^\n]*\n){0,3}', body):
            self.assertNotRegex(m.group(0), r"(?i)\bsuggested trim\b|\bsell\b|\bbuy\b|\btrim\b")


class AckHandler(unittest.TestCase):
    """RETEST-044: the acknowledgment screen must not close on a failed save, must say so, and must never stack."""
    def test_the_screen_closes_only_when_the_server_confirms_and_never_stacks(self):
        body = HTML[HTML.index("function ackCheck(){"):HTML.index("\nfunction ", HTML.index("function ackCheck(){") + 20)]
        self.assertIn("j.ok===true", body)                                              # closes only on a confirmed save
        self.assertIn("ok.disabled=false; msg.textContent=", body)                      # a failure re-enables the button and says so
        self.assertEqual(body.count("document.getElementById('ack-overlay')"), 2)        # checked before and after the fetch: no second screen
        self.assertNotIn(".then(()=>ov.remove())", body)                                # the old unconditional close is gone


class Surfaces3to5and7(unittest.TestCase):
    def test_conviction_cells_and_note_header_carry_the_lead_wording(self):
        self.assertIn("N_LEAD+' '+(b.thesis||'')", HTML)                         # sleeve table cell tooltip
        self.assertRegex(HTML, r"conviction <b>'\+dots\+' '\+N\.conviction\+'/5</b> <span class=\"muted\" title=\"'\+esc\(N_LEAD\)")   # note header

    def test_strategist_page_and_chat_carry_the_paper_wording(self):
        self.assertIn("${N_PAPER} · started", HTML)
        self.assertIn("{ const pn=el('div','muted',N_PAPER);", HTML)               # above the report
        self.assertIn("if(CHAT_PERSONA==='strategist'){ const pn=el('div','muted',N_PAPER)", HTML)

    def test_estimate_notice_is_on_the_tax_type_panels(self):
        m = re.search(r"const NOTICE_ESTIMATE_KEYS=new Set\(\[(.*?)\]\)", HTML)
        keys = set(re.findall(r"'(\w+)'", m.group(1)))
        self.assertTrue({"tax", "ss", "bridge", "estate", "insurance", "roth", "benefits"} <= keys)
        self.assertIn("if(NOTICE_ESTIMATE_KEYS.has(sec.key))", HTML)

    def test_name_sentence_on_the_sign_in_page_and_in_the_readme(self):
        self.assertIn("const nm=el('div','muted',N_NAME)", HTML.split("function _authFormShell")[1].split("function renderFirstRun")[0])
        self.assertIn(notices.NAME, README)


try:
    import app
except Exception:
    app = None


class Req:
    def __init__(self, uid):
        self.session = {"uid": uid}
        self.headers, self.client = {}, None
        self.state = type("S", (), {})()


@unittest.skipIf(app is None, "app not importable")
class Surface6Acknowledgment(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(app, "ACK_FILE", os.path.join(_TMP, "ack-%d.json" % id(self))), mock.patch.object(app.audit, "emit"),
                  mock.patch.object(app.auth, "get_user_by_id", side_effect=lambda uid: {"id": uid, "role": "admin" if uid == "a" else "viewer"} if uid else None),
                  mock.patch.object(app.auth, "is_admin", side_effect=lambda u: u.get("role") == "admin")]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()

    def test_admin_never_sees_it_viewer_sees_it_once(self):
        self.assertFalse(app.get_ack(Req("a"))["required"])
        self.assertTrue(app.get_ack(Req("v"))["required"])
        self.assertTrue(app.post_ack(Req("v"))["ok"])
        self.assertFalse(app.get_ack(Req("v"))["required"])                      # a second sign-in does not show it
        self.assertTrue(app.get_ack(Req("v2"))["required"])                      # per account

    def test_the_acknowledgment_endpoint_is_in_the_viewer_allowlist(self):
        self.assertIn("/api/ack", app._VIEWER_ALLOWED_WRITE_PATHS)

    def test_signed_out_is_refused(self):
        self.assertEqual(app.get_ack(Req(None)).status_code, 401)
        self.assertEqual(app.post_ack(Req(None)).status_code, 401)

    def test_admin_list_shows_who_acknowledged_and_a_viewer_still_cannot_list(self):
        app.post_ack(Req("v"))
        with mock.patch.object(app.auth, "list_users", return_value=[{"id": "a", "username": "admin", "role": "admin"}, {"id": "v", "username": "spouse", "role": "viewer"}, {"id": "w", "username": "kid", "role": "viewer"}]):
            users = {u["id"]: u for u in app.auth_list_users(Req("a"))["users"]}
            self.assertIsNotNone(users["v"]["acknowledged_at"])
            self.assertIsNone(users["w"]["acknowledged_at"])
            self.assertEqual(app.auth_list_users(Req("v")).status_code, 403)


@unittest.skipIf(app is None, "app not importable")
class Surface8DemoShowsSampleDataOnly(unittest.TestCase):
    def test_no_finnhub_call_in_demo_even_with_a_key(self):
        with mock.patch.object(app.demo_seed, "DEMO_MODE", True), mock.patch.object(app, "FINNHUB", "KEY"), \
             mock.patch.object(app.requests, "get", side_effect=AssertionError("network call in demo")):
            self.assertEqual(app._finnhub_json("quote", "AAPL"), (None, False))
            self.assertEqual(app._earnings_calendar() if hasattr(app, "_earnings_calendar") else [], [])

    def test_scavenger_jobs_are_skipped_in_demo(self):
        with mock.patch.object(app.demo_seed, "DEMO_MODE", True):
            for fn in (app._do_universe, app._do_market_data, app._do_signals, app._do_scout):
                self.assertEqual(fn(), "skipped")

    def test_screen_and_scout_reads_are_empty_in_demo_even_when_rows_exist(self):
        import ticker_state
        with mock.patch.dict(os.environ, {"DEMO_MODE": "1"}):
            for name in ("counts", "reason_counts", "list_rows", "screened_rows", "scout_briefs", "list_notes", "picks", "list_rejects", "notes_index", "s2_index"):
                v = getattr(ticker_state, name)(*(["new"] if name == "screened_rows" else []))
                self.assertFalse(v, name)
            self.assertEqual(ticker_state.total(), 0)
            self.assertIsNone(ticker_state.get_note("RKLB"))


if __name__ == "__main__":
    unittest.main()
