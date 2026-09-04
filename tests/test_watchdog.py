"""Acrid Watchdog — stdlib tests. Run: python3 -m unittest discover tests"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("watchdog", HERE / "watchdog.py")
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)


class FileChecks(unittest.TestCase):
    def test_fresh_file_passes_and_stale_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "out.csv"
            p.write_text("a,b\n1,2\n")
            ok, detail = wd.check_file({"path": str(p), "max_age_hours": 1, "min_bytes": 3})
            self.assertTrue(ok, detail)
            old = time.time() - 3 * 3600
            os.utime(p, (old, old))
            ok, detail = wd.check_file({"path": str(p), "max_age_hours": 1})
            self.assertFalse(ok)
            self.assertIn("stale", detail)

    def test_missing_and_too_small(self):
        ok, detail = wd.check_file({"path": "/nonexistent/x.csv"})
        self.assertFalse(ok) and self.assertIn("missing", detail)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tiny"; p.write_text("x")
            ok, detail = wd.check_file({"path": str(p), "min_bytes": 100})
            self.assertFalse(ok); self.assertIn("too small", detail)


class LogChecks(unittest.TestCase):
    def test_patterns(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "job.log"
            p.write_text("start\nbackup complete\n")
            ok, _ = wd.check_log({"path": str(p), "must_match": "backup complete", "must_not_match": "error"})
            self.assertTrue(ok)
            p.write_text("start\nERROR: disk full\n")
            ok, detail = wd.check_log({"path": str(p), "must_match": "backup complete", "must_not_match": "error"})
            self.assertFalse(ok); self.assertIn("error pattern", detail)


class CommandChecks(unittest.TestCase):
    def test_exit_codes(self):
        self.assertTrue(wd.check_command({"command": "true"})[0])
        self.assertFalse(wd.check_command({"command": "false"})[0])


class Helpers(unittest.TestCase):
    def test_json_path_and_env(self):
        self.assertEqual(wd.json_path({"data": [{"updated_at": "x"}]}, "data.0.updated_at"), "x")
        self.assertIsNone(wd.json_path({"data": {}}, "data.missing"))
        os.environ["WD_TEST_TOKEN"] = "abc"
        self.assertEqual(wd.env_or("env:WD_TEST_TOKEN"), "abc")
        self.assertEqual(wd.env_or("literal"), "literal")
        self.assertEqual(wd.env_or(None, "d"), "d")

    def test_nag_clock(self):
        self.assertTrue(wd.nag_due({}, 20))
        recent = wd.iso(datetime.now(timezone.utc) - timedelta(hours=1))
        self.assertFalse(wd.nag_due({"last_paged_at": recent}, 20))
        old = wd.iso(datetime.now(timezone.utc) - timedelta(hours=21))
        self.assertTrue(wd.nag_due({"last_paged_at": old}, 20))

    def test_unknown_type_is_a_failure_not_a_skip(self):
        ok, detail = wd.run_check({"type": "nope"})
        self.assertFalse(ok); self.assertIn("unknown", detail)


class EndToEnd(unittest.TestCase):
    def test_check_status_report_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            good = d / "good.txt"; good.write_text("ok")
            cfg = {"company": "TestCo", "nag_hours": 20, "checks": [
                {"name": "good file", "type": "file", "path": str(good), "max_age_hours": 1},
                {"name": "bad file", "type": "file", "path": str(d / "nope.txt")}]}
            (d / "config.json").write_text(json.dumps(cfg))
            wd.CONFIG_PATH = d / "config.json"; wd.STATE_DIR = d / "state"
            wd.STATE_FILE = wd.STATE_DIR / "state.json"; wd.HISTORY_FILE = wd.STATE_DIR / "history.jsonl"
            wd.KILL_FILE = wd.STATE_DIR / "KILL"; wd.REPORT_DIR = d / "reports"
            class A: days = "7"
            rc = wd.cmd_check(A())
            self.assertEqual(rc, 1)
            st = json.loads(wd.STATE_FILE.read_text())
            self.assertIsNotNone(st["checks"]["bad file"]["failing_since"])
            self.assertEqual(st["checks"]["bad file"]["pages"], 1)
            # second run inside the nag window: no new page
            wd.cmd_check(A())
            st = json.loads(wd.STATE_FILE.read_text())
            self.assertEqual(st["checks"]["bad file"]["pages"], 1)
            # heal it -> recovered, clock reset
            (d / "nope.txt").write_text("back")
            rc = wd.cmd_check(A())
            self.assertEqual(rc, 0)
            st = json.loads(wd.STATE_FILE.read_text())
            self.assertIsNone(st["checks"]["bad file"]["failing_since"])
            self.assertEqual(wd.cmd_status(A()), 0)
            self.assertEqual(wd.cmd_report(A()), 0)
            self.assertTrue(list(wd.REPORT_DIR.glob("delivery-*.md")))


if __name__ == "__main__":
    unittest.main()
