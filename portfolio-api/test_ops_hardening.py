"""RV-B6: read-only Monarch allowlist and pinned bridge packages, backups (exclusions, SQLite-safe, retention), missed-run detection,
heartbeats, local-day budget, trimmed /health."""
import datetime
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as appmod  # noqa: E402
import backup  # noqa: E402
import budget  # noqa: E402
import scavenger  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SRC = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()


class MonarchAllowlist(unittest.TestCase):
    def test_every_post_call_site_uses_an_allowlisted_operation(self):
        ops = set(re.findall(r'\b_post\("([a-z_]+)"', APP_SRC))
        self.assertTrue(ops, "no call sites found")
        self.assertLessEqual(ops, set(appmod.MONARCH_READ_OPS))
        self.assertEqual(set(appmod.MONARCH_READ_OPS), {"get_accounts", "get_account_holdings", "get_cashflow"})

    def test_any_other_operation_is_refused_before_a_request_is_made(self):
        with mock.patch.object(appmod.requests, "post") as post:
            for bad in ("update_transaction", "delete_account", "set_budget_amount", "get_accounts/../x"):
                with self.assertRaises(ValueError):
                    appmod._post(bad, {})
            post.assert_not_called()

    def test_an_allowed_operation_still_goes_through(self):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"accounts": []}
        with mock.patch.object(appmod.requests, "post", return_value=R()) as post, mock.patch.object(appmod, "_monarch_guard", lambda: None):
            self.assertEqual(appmod._post("get_accounts", {}), {"accounts": []})
            self.assertTrue(post.call_args[0][0].endswith("/get_accounts"))

    @unittest.skipUnless(os.path.exists(os.path.join(HERE, "..", "mcpo", "config.json.example")), "repo layout not mounted")
    def test_bridge_packages_are_pinned_to_exact_versions(self):
        cfg = json.load(open(os.path.join(HERE, "..", "mcpo", "config.json.example")))
        for name, srv in cfg["mcpServers"].items():
            pkgs = [a for a in srv["args"] if not a.startswith("-")]
            main = pkgs[0] if name == "time" else pkgs[-1]
            self.assertRegex(main, r"@\d", f"{name}: {main} is not pinned")
        monarch = cfg["mcpServers"]["monarch"]["args"]
        self.assertIn("monarchmoneycommunity==1.7.1", monarch)


class Backups(unittest.TestCase):
    def make_tree(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "universe"))
        os.makedirs(os.path.join(d, "keep", "sub"))
        for rel, data in (("universe/companyfacts.zip", b"Z" * 1000), ("universe/companyfacts.zip.part", b"P"), ("settings.json", b'{"a":1}'),
                          ("keep/sub/note.txt", b"hi"), ("scratch.tmp", b"t"), ("keep/dl.part", b"p")):
            with open(os.path.join(d, rel), "wb") as f:
                f.write(data)
        db = sqlite3.connect(os.path.join(d, "store.db"))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(x)")
        db.execute("INSERT INTO t VALUES ('committed-but-only-in-wal')")
        db.commit()                                    # the row may live only in store.db-wal while the connection stays open
        return d, db

    def names(self, blob):
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            return sorted(m.name for m in tf.getmembers()), tf

    def test_universe_and_temp_files_are_left_out_and_everything_else_kept(self):
        d, db = self.make_tree()
        names, _ = self.names(backup._tar_directory(d))
        db.close()
        self.assertTrue(all(not n.startswith("./universe") for n in names), names)
        self.assertNotIn("./scratch.tmp", names)
        self.assertNotIn("./keep/dl.part", names)
        for want in ("./settings.json", "./keep/sub/note.txt", "./store.db", "./keep", "./keep/sub"):
            self.assertIn(want, names)
        self.assertFalse([n for n in names if n.endswith(("-wal", "-shm"))])

    def test_sqlite_database_is_a_consistent_snapshot_including_wal_data(self):
        d, db = self.make_tree()
        blob = backup._tar_directory(d)
        db.close()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            raw = tf.extractfile("./store.db").read()
        out = os.path.join(tempfile.mkdtemp(), "x.db")
        open(out, "wb").write(raw)
        self.assertEqual(sqlite3.connect(out).execute("SELECT x FROM t").fetchall(), [("committed-but-only-in-wal",)])

    def test_round_trip_and_retention(self):
        d, db = self.make_tree()
        out = tempfile.mkdtemp()
        old = os.path.join(out, "fiduciary-backup-20200101T000000Z.tar.gz.enc")
        open(old, "wb").write(b"old")
        os.utime(old, (time.time() - 100 * 86400,) * 2)
        r = backup.run_backup("pw", d, out, retention_days=60)
        db.close()
        self.assertIn("fiduciary-backup-20200101T000000Z.tar.gz.enc", r["pruned"])
        self.assertTrue(os.path.exists(r["path"]))
        names, _ = self.names(backup._decrypt(open(r["path"], "rb").read(), "pw"))
        self.assertNotIn("./universe/companyfacts.zip", names)

    def test_status_reports_the_retention_in_force_and_the_exclusions(self):
        with mock.patch.object(appmod.backup, "list_backups", lambda d: []), mock.patch.object(appmod, "_read_secrets", lambda: {}):
            r = appmod.get_backup_status()
        self.assertEqual((r["retention_days"], r["sqlite_snapshots"]), (60, True))
        self.assertIn("universe", r["excluded"])

    @unittest.skipUnless(os.path.exists(os.path.join(HERE, "..", "scripts", "backup.sh")), "repo layout not mounted")
    def test_cli_script_excludes_universe_and_defaults_to_sixty_days(self):
        sh = open(os.path.join(HERE, "..", "scripts", "backup.sh")).read()
        self.assertIn("--exclude=./universe", sh)
        self.assertIn("BACKUP_RETENTION_DAYS:-60", sh)
        self.assertIn("BACKUP_VIA_CONTAINER", sh)

    def test_leftover_download_files_are_removed_at_job_start(self):
        d = tempfile.mkdtemp()
        for n in ("companyfacts.zip.part", "companyfacts.zip", "tickers.json"):
            open(os.path.join(d, n), "wb").write(b"x")
        self.assertEqual(sorted(scavenger.cleanup_leftovers(d)), ["companyfacts.zip", "companyfacts.zip.part"])
        self.assertEqual(os.listdir(d), ["tickers.json"])
        self.assertEqual(scavenger.cleanup_leftovers("/no/such/dir"), [])


SCHED = {"universe": {"label": "Universe", "hour": 3, "minute": 0, "dom": "*", "dow": "*", "cadence": "daily"},
         "strategist": {"label": "Strategist", "hour": 19, "minute": 0, "dom": "*", "dow": "4", "cadence": "weekly · Friday"},
         "never": {"label": "Never ran", "hour": 3, "minute": 0, "dom": "*", "dow": "*", "cadence": "daily"}}


def ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


class MissedRuns(unittest.TestCase):
    now = datetime.datetime(2026, 11, 16, 10, 0)            # a Monday

    def hist(self, **kinds):
        return {k: [{"status": st, "finished": ts(f), "started": ts(f) - 60} for st, f in runs] for k, runs in kinds.items()}

    def test_a_daily_job_that_skipped_a_night_is_reported(self):
        h = self.hist(universe=[("done", "2026-11-14 03:10")])
        m = appmod._missed_runs(self.now, SCHED, h)
        self.assertEqual([(x["kind"], x["due"], x["last_success"]) for x in m], [("universe", "2026-11-16 03:00", "2026-11-14 03:10")])

    def test_a_job_that_ran_after_its_due_time_is_fine(self):
        h = self.hist(universe=[("done", "2026-11-16 03:12")])
        self.assertEqual(appmod._missed_runs(self.now, SCHED, h), [])

    def test_errors_and_skips_do_not_count_as_success(self):
        h = self.hist(universe=[("done", "2026-11-13 03:10"), ("error", "2026-11-16 03:05"), ("skipped", "2026-11-16 03:06")])
        self.assertEqual([x["kind"] for x in appmod._missed_runs(self.now, SCHED, h)], ["universe"])

    def test_within_the_grace_period_nothing_is_reported_yet(self):
        h = self.hist(universe=[("done", "2026-11-15 03:10")])
        self.assertEqual(appmod._missed_runs(datetime.datetime(2026, 11, 16, 3, 30), SCHED, h), [])

    def test_jobs_with_no_successful_history_are_not_flagged_and_weekly_jobs_use_their_weekday(self):
        h = self.hist(strategist=[("done", "2026-11-06 19:40")])                 # Friday the 6th; due again Friday the 13th
        m = appmod._missed_runs(self.now, SCHED, h)
        self.assertEqual([(x["kind"], x["due"]) for x in m], [("strategist", "2026-11-13 19:00")])
        self.assertNotIn("never", [x["kind"] for x in m])

    def test_the_attention_list_carries_it_with_a_link_to_the_schedule(self):
        miss = [{"kind": "universe", "label": "Universe & first screen", "cadence": "daily", "due": "2026-11-16 03:00", "last_success": "2026-11-14 03:10"}]
        with mock.patch.object(appmod, "_missed_runs", lambda *a, **k: miss):
            digest = appmod._alerts_digest()
        a = [x for x in digest["alerts"] if x["category"] == "Scheduled jobs"][0]
        self.assertIn("has not succeeded since 2026-11-14 03:10", a["message"])
        self.assertEqual((a["tab"], a["subtab"]), ("settings", "schedule"))

    def test_a_silent_scheduler_thread_is_reported(self):
        with mock.patch.object(appmod, "_missed_runs", lambda *a, **k: []), mock.patch.object(appmod, "INTERNAL_SCHEDULER", True), \
                mock.patch.dict(appmod._SCHED_BEAT, {"scheduler": time.time() - 900}):
            digest = appmod._alerts_digest()
        self.assertTrue([x for x in digest["alerts"] if "scheduler has not checked in" in x["message"]])

    def test_jobs_endpoint_exposes_heartbeats_and_last_success(self):
        with mock.patch.dict(appmod._SCHED_BEAT, {"scheduler": time.time() - 5, "worker": time.time() - 12}):
            r = appmod.get_jobs()
        self.assertLessEqual(r["heartbeat"]["scheduler_age_s"], 10)
        self.assertLessEqual(r["heartbeat"]["worker_age_s"], 20)
        self.assertIn("last_success_by_kind", r)
        self.assertIn("missed_runs", r)


class LocalDayBudget(unittest.TestCase):
    def test_day_rolls_at_local_midnight_not_utc(self):
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()
        try:
            evening = datetime.datetime(2026, 11, 16, 19, 0).timestamp()         # 19:00 CST = 01:00 UTC the next day
            after_midnight = datetime.datetime(2026, 11, 17, 1, 30).timestamp()
            self.assertEqual(budget._local_day(after_midnight) - budget._local_day(evening), 1)
            same_local_day = datetime.datetime(2026, 11, 16, 23, 59).timestamp()
            self.assertEqual(budget._local_day(evening), budget._local_day(same_local_day))       # a UTC day would have rolled in between
            self.assertAlmostEqual(budget._seconds_to_local_midnight(datetime.datetime(2026, 11, 16, 23, 0).timestamp()), 3600, delta=2)
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()


class Health(unittest.TestCase):
    class Req:
        def __init__(self, uid=None):
            self.session = {"uid": uid} if uid else {}

    def test_anonymous_callers_get_only_ok_and_build_ids(self):
        r = appmod.health(self.Req())
        self.assertEqual(set(r), {"ok", "build", "code_build"})

    def test_a_signed_in_user_still_gets_the_detail(self):
        r = appmod.health(self.Req("u1"))
        for k in ("llm_model", "analysis_model", "mcpo", "embed_dialect"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main()
