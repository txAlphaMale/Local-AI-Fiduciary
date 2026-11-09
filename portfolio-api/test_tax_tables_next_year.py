"""Next-year tax figures (tax_updates/2027.json): shape, honesty about status, display, monthly reminder. Not used by any engine."""
import datetime, glob, json, os, re, sys, tempfile, unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="taxtables-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import grounding  # noqa: E402
import app as appmod  # noqa: E402

DATA = json.load(open(os.path.join(_HERE, "tax_updates", "2027.json"), encoding="utf-8"))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
REG_KEYS = {s["key"] for s in grounding.PARAM_SPECS}


class TestFile(unittest.TestCase):
    def test_every_item_maps_to_a_registry_entry_with_a_valid_status_and_https_source(self):
        for it in DATA["items"]:
            self.assertIn(it["key"], REG_KEYS, it["key"])
            self.assertIn(it["status"], ("primary", "secondary", "pending", "statutory"), it["key"])
            self.assertTrue(it["source_url"].startswith("https://"), it["key"])
            self.assertTrue(it["summary"].strip())
        self.assertEqual(len({i["key"] for i in DATA["items"]}), len(DATA["items"]))

    def test_primary_items_cite_an_official_government_source(self):
        for it in DATA["items"]:
            if it["status"] == "primary":
                self.assertRegex(it["source_url"], r"^https://(www\.irs\.gov|www\.federalregister\.gov)/", it["key"])

    def test_unpublished_figures_are_not_invented(self):
        by = {i["key"]: i for i in DATA["items"]}
        self.assertNotIn("values", by["roth.irmaa"])                              # CMS has not published the 2027 IRMAA brackets
        v = by["benefits.contribution_limits"]["values"]
        self.assertEqual(set(v), {"hsa_self", "hsa_family", "hdhp_min_deductible_self", "hdhp_min_deductible_family",
                                  "hdhp_oop_max_self", "hdhp_oop_max_family"})   # only the HSA figures the IRS has published
        self.assertFalse([k for k in v if "401" in k or "ira" in k])

    def test_key_figures_match_the_documents_they_were_read_from(self):
        by = {i["key"]: i["values"] for i in DATA["items"] if "values" in i}
        self.assertEqual(by["tax.standard_deduction"]["mfj"], 33200)
        self.assertEqual(by["tax.ordinary_brackets"]["mfj"][1], [0.12, 25600])
        self.assertEqual(by["tax.ordinary_brackets"]["single"][-1], [0.37, 661375])
        self.assertEqual(by["estate.exemption"]["exemption"], 15480000)
        self.assertEqual(by["estate.annual_exclusion"]["exclusion"], 20000)
        self.assertEqual(by["aca.applicable_pct"]["required_contribution_pct"], 10.22)
        self.assertEqual(by["aca.fpl"]["48_states"][0], 15960)

    def test_brackets_are_sane(self):
        for status, rows in {i["key"]: i["values"] for i in DATA["items"] if "values" in i}["tax.ordinary_brackets"].items():
            rates = [r for r, _ in rows]; lows = [lo for _, lo in rows]
            self.assertEqual(rates, sorted(rates)); self.assertEqual(lows, sorted(lows)); self.assertEqual(lows[0], 0)


class TestModuleAndUi(unittest.TestCase):
    def test_next_year_loads_only_for_the_next_calendar_year_and_is_not_active(self):
        n = grounding.next_year(datetime.date(2026, 11, 9))
        self.assertEqual(n["year"], 2027); self.assertFalse(n["active"]); self.assertEqual(sum(n["counts"].values()), len(DATA["items"]))
        self.assertTrue(all(i["label"] for i in n["items"]))
        self.assertEqual([i["status"] for i in n["items"]][0], "pending")           # unfinished items first
        self.assertIsNone(grounding.next_year(datetime.date(2030, 1, 1)))
        self.assertIn("next_year", grounding.summary(datetime.date(2026, 11, 9)))

    def test_no_engine_reads_the_proposal_file(self):
        for mod in ("tax.py", "aca.py", "ss.py", "estate.py", "roth.py", "benefits.py"):
            self.assertNotIn("tax_updates", open(os.path.join(_HERE, mod), encoding="utf-8").read(), mod)

    def test_ui_shows_status_labels_and_wiki_entries_exist(self):
        for needle in ("NEXT YEAR · '+ny.year+' FIGURES (RESEARCHED, NOT YET IN USE)", "VERIFIED", "CONFIRM", "PENDING", "NO CHANGE",
                       "W('roth-vs-trad'", "W('tax-tables-annual'", "W('chat-notes'", "W('dash-arrange'"):
            self.assertIn(needle, HTML, needle)


class TestReminder(unittest.TestCase):
    def setUp(self):
        self.f = os.path.join(_TMP, "notified.json")
        if os.path.exists(self.f):
            os.remove(self.f)

    def run_sweep(self, day):
        with mock.patch.object(appmod, "TAX_REMINDER_FILE", self.f), mock.patch.object(appmod.notifications, "notify_reminder") as m:
            r = appmod._tax_tables_reminder_sweep(day)
        return r, m

    def test_one_reminder_per_month_in_season_only(self):
        r, m = self.run_sweep(datetime.date(2026, 11, 9))
        self.assertTrue(r); self.assertEqual(m.call_count, 1); self.assertIn("2027 figures", m.call_args[0][1])
        r2, m2 = self.run_sweep(datetime.date(2026, 11, 20))
        self.assertIsNone(r2); self.assertEqual(m2.call_count, 0)                  # same month: no second message
        r3, m3 = self.run_sweep(datetime.date(2026, 12, 1)); self.assertEqual(m3.call_count, 1)
        r4, m4 = self.run_sweep(datetime.date(2026, 6, 1)); self.assertIsNone(r4); self.assertEqual(m4.call_count, 0)   # out of season


if __name__ == "__main__":
    unittest.main()
