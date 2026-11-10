"""Adjustable table columns (CASH CHECK first): fixed layout + colgroup + a drag handle per header, widths saved as a synced preference."""
import os, re, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class TestResizableColumns(unittest.TestCase):
    def test_helper_and_wiring_exist(self):
        for needle in ("function resizableTable(table, id, defaultPct)", "function colWidthsLoad(id)", "function colWidthsSave(id,arr)",
                       "resizableTable(t,'cashcheck-accounts',", "resizableTable(t2,'cashcheck-holdings',", "RESET COLUMN WIDTHS",
                       "table.style.tableLayout='fixed'", "h.setPointerCapture(ev.pointerId)", "h.className='col-rs'"):
            self.assertIn(needle, HTML, needle)

    def test_default_percentages_cover_every_column_and_sum_to_100(self):
        for m in re.finditer(r"resizableTable\(t2?,'cashcheck-[a-z]+',\[([0-9,]+)\]\)", HTML):
            pct = [int(x) for x in m.group(1).split(",")]
            self.assertEqual(sum(pct), 100, pct)
        heads = re.findall(r"<th[ >]", HTML.split("function cashCheckContent()")[1].split("function _errorsRefresh()")[0])
        self.assertEqual(len(heads), 8 + 8 + 5)                                      # 8 account + 8 holding + 5 'what the donuts count' columns

    def test_widths_follow_the_account_like_other_layout_preferences(self):
        src = open(os.path.join(_HERE, "prefs.py"), encoding="utf-8").read()     # parsed as text: importing prefs needs the data-dir env other tests set up
        self.assertIn('"fidColWidths"', src.split("SYNC_KEYS = frozenset(")[1].split("})")[0])
        self.assertIn("'fidColWidths'", re.search(r"const PREFS_KEYS=\[(.*?)\];", HTML).group(1))

    def test_cells_wrap_and_the_handle_is_hidden_when_printing_and_does_not_use_inline_handlers(self):
        self.assertIn("tr.wrapcells td{white-space:normal;", HTML)
        self.assertIn(".col-rs{display:none !important;}", HTML)
        helper = HTML.split("function resizableTable")[1].split("function colWidthsResetBtn")[0]
        self.assertNotRegex(helper, r"\bon[a-z]+\s*=")                              # listeners only (CSP script-src-attr 'none')

    def test_minimum_width_and_scroll_container(self):
        helper = HTML.split("function resizableTable")[1].split("function colWidthsResetBtn")[0]
        self.assertIn("Math.max(50,", helper); self.assertIn("overflow-x:auto", helper)


if __name__ == "__main__":
    unittest.main()
