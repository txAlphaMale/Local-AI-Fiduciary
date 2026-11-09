"""New detection rules: one IP trying many usernames; markup-like attempted names. Pure logic: audit is stubbed."""
import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import detections  # noqa: E402


def row(name, ip):
    return {"actor_name": name, "src_ip": ip}


class Spray(unittest.TestCase):
    def run_rule(self, fn, rows, already=False):
        emitted = []
        with mock.patch.object(detections.audit, "query_core", side_effect=lambda **k: rows if k.get("event_type") == "auth.login.failure" else ([1] if already else [])), \
             mock.patch.object(detections.audit, "emit", side_effect=lambda *a, **k: emitted.append((a, k))):
            return fn(), emitted

    def test_five_names_one_ip_alerts_without_names(self):
        rows = [row(f"user{i}", "203.0.113.9") for i in range(5)]
        fired, em = self.run_rule(detections._check_login_spray_from_ip, rows)
        self.assertEqual(fired, ["detection:login_spray:203.0.113.9"])
        self.assertEqual(em[0][1]["detail"]["distinct_usernames"], 5)
        self.assertNotIn("user0", str(em))

    def test_four_names_or_one_name_repeated_does_not(self):
        self.assertEqual(self.run_rule(detections._check_login_spray_from_ip, [row(f"u{i}", "1.1.1.1") for i in range(4)])[0], [])
        self.assertEqual(self.run_rule(detections._check_login_spray_from_ip, [row("admin", "1.1.1.1")] * 20)[0], [])

    def test_dedupe(self):
        rows = [row(f"u{i}", "1.1.1.1") for i in range(6)]
        self.assertEqual(self.run_rule(detections._check_login_spray_from_ip, rows, already=True)[0], [])

    def test_markup_name_alerts_once_per_ip_and_hides_the_name(self):
        rows = [row('<img src=x onerror=alert(1)>', "198.51.100.7"), row("javascript:alert(1)", "198.51.100.7"), row("alice", "198.51.100.8")]
        fired, em = self.run_rule(detections._check_suspicious_login_name, rows)
        self.assertEqual(fired, ["detection:suspicious_name:198.51.100.7"])
        self.assertEqual(em[0][1]["detail"]["attempts"], 2)
        self.assertNotIn("onerror", str(em))

    def test_plain_names_do_not_alert(self):
        self.assertEqual(self.run_rule(detections._check_suspicious_login_name, [row("alice", "1.1.1.1"), row("bob.smith", "1.1.1.1")])[0], [])

    def test_registered_in_rule_table_and_runner(self):
        ids = {r["id"] for r in detections.RULES}
        self.assertTrue({"login_spray_from_ip", "suspicious_login_name"} <= ids)


if __name__ == "__main__":
    unittest.main()
