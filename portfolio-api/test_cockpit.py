# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""test_cockpit.py -- TASK-004: dismissible green cockpit tiles (server store + the visibility rule)."""
import glob, json, os, re, shutil, subprocess, sys, tempfile, unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="cockpit-test-")
for _f in glob.glob(os.path.join(_HERE, "*.py")):
    if os.path.basename(_f).startswith("test_"):
        continue
    for _m in re.finditer(r'environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"(/data[^"]*)"', open(_f).read()):
        os.environ.setdefault(_m.group(1), os.path.join(_TMP, _m.group(2)[len("/data"):].lstrip("/")))
os.environ["COCKPIT_DISMISSED_FILE"] = os.path.join(_TMP, "cockpit_dismissed.json")
os.environ.setdefault("INTERNAL_SCHEDULER", "0")
sys.path.insert(0, _HERE)
from fastapi.testclient import TestClient  # noqa: E402
import app as appmod, auth, store  # noqa: E402

PW = "correct-horse-battery-1"


def _login(u):
    c = TestClient(appmod.app)
    assert c.post("/api/auth/login", json={"username": u, "password": PW}).status_code == 200
    return c


class TestDismissApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        appmod.COCKPIT_DISMISSED_FILE = os.environ["COCKPIT_DISMISSED_FILE"]
        for n, mk in (("ck_admin", lambda n: auth.create_user(n, PW, role="admin")), ("ck_viewer", lambda n: auth.create_viewer(n, PW))):
            if not auth.get_user_by_username(n):
                mk(n)
        cls.A, cls.V = _login("ck_admin"), _login("ck_viewer")

    def setUp(self):
        self.A.post("/api/cockpit/restore", json={"all": True}); self.V.post("/api/cockpit/restore", json={"all": True})

    def test_only_green_tiles_can_be_dismissed(self):
        for sev in ("warn", "crit", "info", "", None):
            self.assertEqual(self.A.post("/api/cockpit/dismiss", json={"id": "X", "sev": sev}).status_code, 400, sev)
        self.assertEqual(self.A.post("/api/cockpit/dismiss", json={"id": "FIRE", "sev": "ok"}).status_code, 200)
        self.assertEqual(self.A.post("/api/cockpit/dismiss", json={"id": "TAX-LOSS", "sev": "opp"}).status_code, 200)
        self.assertEqual(self.A.get("/api/cockpit/dismissed").json()["dismissed"], {"FIRE": "ok", "TAX-LOSS": "opp"})
        self.assertEqual(self.A.post("/api/cockpit/dismiss", json={"sev": "ok"}).status_code, 400)

    def test_per_user_isolation_and_viewer_self_service(self):
        self.A.post("/api/cockpit/dismiss", json={"id": "FIRE", "sev": "ok"})
        self.assertEqual(self.V.get("/api/cockpit/dismissed").json()["dismissed"], {})
        r = self.V.post("/api/cockpit/dismiss", json={"id": "EMERGENCY FUND", "sev": "ok"})
        self.assertEqual(r.status_code, 200)                                  # viewer may manage THEIR OWN
        self.assertEqual(self.A.get("/api/cockpit/dismissed").json()["dismissed"], {"FIRE": "ok"})

    def test_restore_ids_and_all(self):
        for t in ("A", "B", "C"):
            self.A.post("/api/cockpit/dismiss", json={"id": t, "sev": "ok"})
        self.A.post("/api/cockpit/restore", json={"ids": ["A", "B"]})
        self.assertEqual(self.A.get("/api/cockpit/dismissed").json()["dismissed"], {"C": "ok"})
        self.A.post("/api/cockpit/restore", json={"all": True})
        self.assertEqual(self.A.get("/api/cockpit/dismissed").json()["dismissed"], {})

    def test_unauthenticated_denied_and_viewer_allowlist_is_exactly_these(self):
        anon = TestClient(appmod.app)
        self.assertEqual(anon.get("/api/cockpit/dismissed").status_code, 401)
        self.assertEqual(anon.post("/api/cockpit/dismiss", json={"id": "x", "sev": "ok"}).status_code, 401)
        self.assertTrue({"/api/cockpit/dismiss", "/api/cockpit/restore"} <= appmod._VIEWER_ALLOWED_WRITE_PATHS)

    def test_state_is_stored_encrypted_in_the_store_not_a_plain_file(self):
        self.A.post("/api/cockpit/dismiss", json={"id": "SECRET-TILE-NAME", "sev": "ok"})
        self.assertFalse(os.path.exists(os.environ["COCKPIT_DISMISSED_FILE"]))


NODE = shutil.which("node")


def _vis_js():
    s = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
    i = s.index("function cockpitVisibility(")
    j = s.index("function _cockpitRerender(")
    return s[i:j]


@unittest.skipUnless(NODE, "node not installed")
class TestVisibilityRule(unittest.TestCase):
    def run_js(self, A, dism):
        code = _vis_js() + f"\nconsole.log(JSON.stringify(cockpitVisibility({json.dumps(A)}, {json.dumps(dism)})));"
        out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, check=True).stdout
        r = json.loads(out)
        return [a["cat"] for a in r["shown"]], [a["cat"] for a in r["hidden"]], sorted(r["prune"])

    def test_green_dismissed_is_hidden(self):
        A = [{"cat": "FIRE", "sev": "ok"}, {"cat": "CONC", "sev": "warn"}]
        self.assertEqual(self.run_js(A, {"FIRE": "ok"}), (["CONC"], ["FIRE"], []))

    def test_reappears_when_it_stops_being_green_and_is_pruned(self):
        A = [{"cat": "EMERGENCY FUND", "id": "EMERGENCY FUND", "sev": "warn"}]
        shown, hidden, prune = self.run_js(A, {"EMERGENCY FUND": "ok"})
        self.assertEqual((shown, hidden, prune), (["EMERGENCY FUND"], [], ["EMERGENCY FUND"]))

    def test_absent_tile_dismissal_is_pruned_so_it_is_fresh_when_it_returns(self):
        self.assertEqual(self.run_js([], {"TAX-LOSS": "opp"}), ([], [], ["TAX-LOSS"]))

    def test_non_green_is_never_hidden_even_if_stored(self):
        A = [{"cat": "BETA", "sev": "warn"}]
        self.assertEqual(self.run_js(A, {"BETA": "warn"})[0], ["BETA"])

    def test_opp_and_ok_are_distinct(self):
        A = [{"cat": "TAX-LOSS", "sev": "opp"}]
        self.assertEqual(self.run_js(A, {"TAX-LOSS": "ok"}), (["TAX-LOSS"], [], ["TAX-LOSS"]))


class TestEmergencyTileRule(unittest.TestCase):
    """The R/Y/G mapping lives in index.html; assert its shape so a refactor can't silently drop a state."""
    def test_mapping_present(self):
        s = open(os.path.join(_HERE, "static", "index.html"), encoding="utf-8").read()
        i = s.index("TASK-004: EMERGENCY FUND")
        block = s[i:i + 1400]
        for needle in ("'crit'", "'warn'", "'ok'", "status_stress", "ef.gap_dollars", "subtab:'cashdebt'", "estimate (no accounts designated)"):
            self.assertIn(needle, block)


if __name__ == "__main__":
    unittest.main()
