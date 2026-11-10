"""Decision notes + chat memory tools: encrypted at rest, revisit reminders, read-only chat tools, admin-only writes."""
import glob, os, re, sqlite3, sys, tempfile, unittest, datetime
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="notes-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
import notes  # noqa: E402
import app as appmod  # noqa: E402

APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class TestNotesStore(unittest.TestCase):
    def setUp(self):
        with notes._conn() as c:
            c.execute("DELETE FROM notes")

    def test_round_trip_and_encrypted_at_rest(self):
        nid = notes.add("Rollover plan", "Move 60% to total-market funds", question="how to rebalance?", session_id="abc",
                        snapshot={"net_worth": 1}, revisit_on="2027-01-15", revisit_event="new employer details")
        n = notes.get(nid)
        self.assertEqual((n["title"], n["revisit_on"], n["status"]), ("Rollover plan", "2027-01-15", "open"))
        raw = sqlite3.connect(notes.NOTES_DB).execute("SELECT title, body, question, snapshot, revisit_event FROM notes").fetchone()
        for cell in raw:
            self.assertNotIn("Rollover", cell or "")
            self.assertNotIn("total-market", cell or "")

    def test_validation(self):
        with self.assertRaises(ValueError): notes.add("", "x")
        with self.assertRaises(ValueError): notes.add("t", "")
        with self.assertRaises(ValueError): notes.add("t", "x", revisit_on="next tuesday")
        nid = notes.add("t", "b")
        with self.assertRaises(ValueError): notes.update(nid, status="weird")
        with self.assertRaises(ValueError): notes.update(nid)

    def test_due_and_reminded_once_and_rearmed_by_new_date(self):
        a = notes.add("past", "b", revisit_on="2020-01-01")
        b = notes.add("future", "b", revisit_on="2999-01-01")
        c_ = notes.add("nodate", "b")
        d = notes.add("done", "b", revisit_on="2020-01-01"); notes.update(d, status="done")
        self.assertEqual([n["id"] for n in notes.due()], [a])
        notes.mark_reminded(a)
        self.assertEqual(notes.due(), [])
        notes.update(a, revisit_on="2020-02-02")          # a new date re-arms the reminder
        self.assertEqual([n["id"] for n in notes.due()], [a])

    def test_sweep_sends_one_reminder_per_due_note(self):
        notes.add("Review 401k split", "b", revisit_on="2020-01-01", revisit_event="new plan arrived")
        with mock.patch.object(appmod.notifications, "notify_reminder") as m:
            appmod._notes_reminder_sweep(); appmod._notes_reminder_sweep()
        self.assertEqual(m.call_count, 1)
        self.assertIn("Review 401k split", m.call_args[0][0])

    def test_delete(self):
        nid = notes.add("t", "b")
        self.assertTrue(notes.delete(nid)); self.assertFalse(notes.delete(nid)); self.assertIsNone(notes.get(nid))


class TestChatTools(unittest.TestCase):
    def test_tools_registered_for_fiduciary_only_and_read_only(self):
        names = [t["function"]["name"] for t in appmod.FIDUCIARY_TOOLS]
        self.assertIn("get_notes", names); self.assertIn("search_past_chats", names)
        self.assertNotIn("save_note", names)
        self.assertFalse([n for n in names if n.startswith(("add_", "save_", "delete_", "update_"))])
        self.assertNotIn("get_notes", [t["function"]["name"] for t in appmod.STRATEGIST_ADVICE_TOOLS])

    def test_get_notes_tool_shows_saved_figures_next_to_today(self):
        with notes._conn() as c: c.execute("DELETE FROM notes")
        notes.add("401k election", "Consider a 50/50 split", question="pre vs roth?", snapshot={"net_worth": 111})
        r = appmod._fiduciary_dispatch("get_notes", {})
        self.assertEqual(r["notes"][0]["title"], "401k election")
        self.assertEqual(r["notes"][0]["figures_when_saved"]["net_worth"], 111)
        self.assertIn("figures_today", r)

    def test_search_past_chats_finds_across_sessions_and_decrypts(self):
        s1 = appmod._chat_create_session("fiduciary", "Rollover chat")
        appmod._chat_add_message(s1, "user", "How should I rebalance the rollover IRA?")
        appmod._chat_add_message(s1, "assistant", "Trim the concentrated position in ZZZQ over two tax years.")
        s2 = appmod._chat_create_session("fiduciary", "Other")
        appmod._chat_add_message(s2, "user", "unrelated question about insurance")
        r = appmod._fiduciary_dispatch("search_past_chats", {"query": "rollover rebalance"})
        self.assertEqual({m["session_id"] for m in r["matches"]}, {s1})
        r2 = appmod._fiduciary_dispatch("search_past_chats", {"query": "zzzq"})
        self.assertTrue(r2["matches"] and "ZZZQ" in r2["matches"][0]["snippet"])
        self.assertIn("error", appmod._fiduciary_dispatch("search_past_chats", {"query": " "}))

    def test_prompt_mentions_memory_rules(self):
        self.assertIn("SAVED NOTES", appmod.FIDUCIARY_PROMPT)
        self.assertIn("SAVE NOTE", appmod.FIDUCIARY_PROMPT)


class TestAccessAndUi(unittest.TestCase):
    def test_note_writes_are_not_viewer_allowed(self):
        allowed = appmod._VIEWER_ALLOWED_WRITE_PATHS
        self.assertFalse([p for p in allowed if p.startswith("/api/notes")])

    def test_ui_save_button_is_admin_only_and_user_text_is_escaped(self):
        self.assertIn("_isAdminUser() && CHAT_PERSONA==='fiduciary'", HTML)
        self.assertIn('value="\'+escHtml(NOTE_FORM.title)+\'"', HTML)
        self.assertIn("b.innerHTML=mdToHtml(n.body)", HTML)        # hardened renderer, not raw


if __name__ == "__main__":
    unittest.main()


class TestDbFileModes(unittest.TestCase):
    """The small chat-memory databases are owner-only (0600), like store/auth/audit; they used to be 0644."""
    def test_modes(self):
        import prefs
        for opener, path in ((notes._conn, notes.NOTES_DB), (prefs._conn, prefs.PREFS_DB), (appmod._chat_conn, appmod.CHAT_DB)):
            if os.path.exists(path):
                os.chmod(path, 0o644)
            opener().close()
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, path)
