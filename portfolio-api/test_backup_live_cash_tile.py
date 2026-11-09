import io, os, contextlib, re, unittest

HERE = os.path.dirname(__file__)
APP = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
HTML = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()


class TestSmallFixes(unittest.TestCase):
    def test_backup_passphrase_is_live_so_no_rebuild_message(self):
        m = re.search(r"_LIVE_SECRETS = \{(.*?)\}", APP, re.S)
        self.assertIn('"BACKUP_PASSPHRASE"', m.group(1))

    def test_email_failure_log_carries_no_server_text(self):
        import notifications
        orig = notifications.send_email_checked
        notifications.send_email_checked = lambda s, b: (False, "Send failed: SMTPException: secret-server-reply")
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                notifications._send_email("s", "b")
        finally:
            notifications.send_email_checked = orig
        self.assertIn("email send failed", buf.getvalue())
        self.assertNotIn("secret-server-reply", buf.getvalue())

    def test_invested_tile_excludes_cash(self):
        self.assertIn('"cash_value"', APP)
        self.assertIn("(d.invested_value||0)-(d.cash_value||0)", HTML)


if __name__ == "__main__":
    unittest.main()
