"""RV-T5 / TASK-RV-039: the quarterly household report. Content comes only from the engines' outputs; no account numbers; changes between reports are named;
a down source shows last-known values with their date; role rules: viewers read (while allowed), only admins generate, delete or configure."""
import asyncio, glob, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="hhr-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    with open(_f) as _fh:
        for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', _fh.read()):
            os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import household_report as hr  # noqa: E402

NOW = 1_800_000_000


def src(nw=100000.0, accounts=None, **over):
    accounts = accounts if accounts is not None else [
        {"name": "Fidelity Brokerage 123456789", "institution": "Fidelity", "type": "Investments", "balance": 90000.0, "is_asset": True, "include_nw": True},
        {"name": "Home loan", "institution": "Bank", "type": "Mortgage", "balance": 10000.0, "is_asset": False, "include_nw": True}]
    s = {"data": {"net_worth": nw, "total_assets": nw + 10000.0, "total_liabilities": 10000.0, "accounts": accounts, "generated_at": NOW},
         "retirement": {"generated_at": NOW, "years_to_fi": 12.0, "fi_year": 2038, "funded_ratio": {"funded_ratio": 0.8, "essential_funded_ratio": 0.9},
                        "monte_carlo": {"success_pct": 71.0, "volatility_pct": 16.0, "holdings_volatility": {"available": True, "measured_pct": 14.0, "success_pct": 65.0}}},
         "alerts": {"generated_at": NOW, "alerts": [{"level": "warn", "message": "One position is over your size limit."}], "counts": {}},
         "cashdebt": {"generated_at": NOW, "emergency_fund": {"months_covered": 4.0, "target_months": 6.0, "status": "low", "gap_dollars": 3000}},
         "insurance": {"gap_table": [{"line": "Life (self)", "need": 500000, "in_force": 100000, "gap": 400000, "status": "gap"}, {"line": "Umbrella", "need": 0, "in_force": 0, "gap": 0, "status": "ok"}]},
         "continuity": {"generated_at": NOW, "executor_name": "A. Person", "will_location": "Safe", "contacts": [{"name": "Lawyer", "role": "attorney", "phone": "555-1234"}],
                        "doc_currency": [], "beneficiary_summary": {}},
         "emergency": {"config": {"enabled": True}}}
    s.update(over)
    return s


class Content(unittest.TestCase):
    def test_every_section_and_the_closing_line(self):
        rep = hr.build(src(), now=NOW)
        for k in hr.SECTIONS:
            self.assertIn(k, rep["sections"])
        self.assertEqual(rep["closing"], hr.CLOSING)
        txt = hr.to_text(rep)
        self.assertTrue(txt.rstrip().endswith(hr.CLOSING))
        self.assertIn("71%", txt)
        self.assertIn("one position is over your size limit", txt.lower())

    def test_no_account_numbers_phone_numbers_or_secrets(self):
        rep = hr.build(src(), now=NOW)
        blob = repr(rep["sections"]) + repr(rep["accounts_snapshot"]) + hr.to_text(rep)
        self.assertNotIn("123456789", blob)
        self.assertNotIn("555-1234", blob)
        self.assertIn("••••", repr(rep["accounts_snapshot"]))

    def test_emergency_contact_only_when_switched_on_in_settings_and_enabled(self):
        off = hr.build(src(), now=NOW, config={"show_emergency_contact": False})
        on = hr.build(src(), now=NOW, config={"show_emergency_contact": True})
        self.assertNotIn("emergency_access", off["sections"]["if_something_happens"])
        self.assertIn("emergency_access", on["sections"]["if_something_happens"])
        none = hr.build(src(emergency={"config": {"enabled": False}}), now=NOW, config={"show_emergency_contact": True})
        self.assertNotIn("emergency_access", none["sections"]["if_something_happens"])


class Changes(unittest.TestCase):
    def test_second_report_names_what_changed(self):
        first = hr.build(src(), now=NOW)
        second = hr.build(src(nw=130000.0, accounts=[
            {"name": "Fidelity Brokerage 123456789", "institution": "Fidelity", "type": "Investments", "balance": 100000.0, "is_asset": True},
            {"name": "Home loan", "institution": "Bank", "type": "Mortgage", "balance": 10000.0, "is_asset": False},
            {"name": "New savings", "institution": "Bank", "type": "Cash", "balance": 25000.0, "is_asset": True}]), now=NOW + 90 * 86400, prev=first)
        n = second["sections"]["net_worth"]
        self.assertEqual(n["change"], 30000.0)
        names = [c["name"] for c in n["biggest_changes"]]
        self.assertEqual(names[0], "New savings")
        self.assertEqual(n["biggest_changes"][0]["kind"], "new")
        self.assertIn("(new)", hr.to_text(second))

    def test_first_report_says_so(self):
        self.assertTrue(hr.build(src(), now=NOW)["sections"]["net_worth"]["first_report"])


class DownSources(unittest.TestCase):
    def test_a_source_that_failed_shows_last_known_values_and_their_date(self):
        first = hr.build(src(), now=NOW)
        later = hr.build(src(retirement={"_error": "HTTP 500"}), now=NOW + 86400, prev=first)
        p = later["sections"]["plan"]
        self.assertTrue(p["from_last_known"])
        self.assertEqual(p["last_known_date"], NOW)
        self.assertIn("last known values", hr.to_text(later))

    def test_nothing_to_fall_back_on_says_unavailable_not_a_made_up_number(self):
        rep = hr.build(src(retirement={"_error": "x"}), now=NOW)
        self.assertTrue(rep["sections"]["plan"]["unavailable"])

    def test_an_old_source_shows_its_age(self):
        rep = hr.build(src(retirement={"generated_at": NOW - 5 * 86400, "years_to_fi": 3.0, "monte_carlo": {}, "funded_ratio": {}}), now=NOW)
        self.assertIn("5 days old", rep["sources"]["plan"]["age_note"])


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
        return self._b


@unittest.skipIf(app is None, "app not importable")
class Roles(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(app, "HHR_FILE", os.path.join(_TMP, "hhr-%d.json" % id(self))),
                  mock.patch.object(app, "_hhr_sources", return_value=src()),
                  mock.patch.object(app, "_emit_config_changed"), mock.patch.object(app.audit, "emit"),
                  mock.patch.object(app.auth, "get_user_by_id", side_effect=lambda uid: {"id": uid, "role": "admin" if uid == "a" else "viewer"} if uid else None),
                  mock.patch.object(app.auth, "is_admin", side_effect=lambda u: u.get("role") == "admin")]
        for x in self.p: x.start()

    def tearDown(self):
        for x in self.p: x.stop()

    def test_viewer_cannot_generate_delete_or_configure(self):
        self.assertEqual(app.post_household_report_generate(Req("v")).status_code, 403)
        self.assertEqual(asyncio.run(app.delete_household_report("r1", Req("v"))).status_code, 403)
        self.assertEqual(asyncio.run(app.post_household_report_config(Req("v", {"viewers_see": False}))).status_code, 403)

    def test_admin_generates_viewer_reads_then_admin_turns_it_off_for_viewers(self):
        r = app.post_household_report_generate(Req("a"))
        self.assertTrue(r["ok"])
        got = app.get_household_report(Req("v"))
        self.assertEqual(len(got["reports"]), 1)
        self.assertNotIn("viewers_see", got["config"])                        # a viewer is not shown the admin settings
        self.assertNotIn("accounts_snapshot", got["latest"])
        asyncio.run(app.post_household_report_config(Req("a", {"viewers_see": False})))
        self.assertEqual(app.get_household_report(Req("v")).status_code, 403)
        self.assertEqual(app.get_household_report_one(r["id"], Req("v")).status_code, 403)
        self.assertEqual(len(app.get_household_report(Req("a"))["reports"]), 1)

    def test_admin_delete_and_bad_config_values(self):
        rid = app.post_household_report_generate(Req("a"))["id"]
        self.assertEqual(asyncio.run(app.post_household_report_config(Req("a", {"viewers_see": "yes"}))).status_code, 400)
        self.assertTrue(asyncio.run(app.delete_household_report(rid, Req("a")))["ok"])
        self.assertEqual(asyncio.run(app.delete_household_report(rid, Req("a"))).status_code, 404)

    def test_run_now_writes_a_report_in_any_month(self):
        with mock.patch.object(app, "_enqueue", return_value=("j1", True)):
            self.assertTrue(app.run_household_report_ep()["created"])
        self.assertTrue(app._HHR_MANUAL.is_set())
        with mock.patch.object(app.notifications, "notify_reminder"), mock.patch.object(app.datetime, "date", wraps=app.datetime.date) as d:
            d.today.return_value = app.datetime.date(2026, 11, 1)
            self.assertIn("id", app._do_household_report())
        self.assertFalse(app._HHR_MANUAL.is_set())

    def test_scheduled_job_writes_only_in_the_first_month_of_a_quarter(self):
        with mock.patch.object(app.notifications, "notify_reminder") as nr:
            with mock.patch.object(app.datetime, "date", wraps=app.datetime.date) as d:
                d.today.return_value = app.datetime.date(2026, 11, 1)
                self.assertIn("skipped", app._do_household_report())
                d.today.return_value = app.datetime.date(2027, 1, 1)
                self.assertIn("id", app._do_household_report())
            msg = nr.call_args[0]
            self.assertNotRegex(msg[0] + msg[1], r"\d{3}")                    # the notification carries no figures


if __name__ == "__main__":
    unittest.main()
