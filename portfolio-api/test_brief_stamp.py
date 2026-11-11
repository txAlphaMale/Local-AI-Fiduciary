"""The finished State-of-the-Wealth brief shows how long generation took (not counting the wait for the GPU)."""
import os, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
APP = open(os.path.join(_HERE, "app.py"), encoding="utf-8").read()
HTML = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()


class TestBriefStamp(unittest.TestCase):
    def test_generation_time_starts_after_the_gpu_is_acquired_and_is_stored(self):
        body = APP.split("def _do_narrative():")[1].split('@app.get("/api/narrative")')[0]
        i_slot, i_start, i_llm, i_end = (body.index(x) for x in ("with _gpu_slot():", "t_gen = time.time()", "llm._llm_chat(", "gen_s = round(time.time() - t_gen, 1)"))
        self.assertTrue(i_slot < i_start < i_llm < i_end)                                 # the clock starts inside the GPU slot
        self.assertIn('"gen_s": gen_s', body)

    def test_ui_shows_generated_in_and_the_gpu_wait_separately(self):
        line = HTML.split("nl.generated_at*1000).toLocaleString()")[1].split("nbox.appendChild(dt)")[0]
        for needle in ("generated in ${fmtDur(nl.gen_s)}", "waited '+fmtDur(nl.gpu_wait_s)+' for the GPU first", "no GPU wait"):
            self.assertIn(needle, line, needle)


if __name__ == "__main__":
    unittest.main()
