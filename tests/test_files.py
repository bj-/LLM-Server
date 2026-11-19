import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import srv_files


class FilePolicyTests(unittest.TestCase):
    def test_large_transfer_requires_explicit_choice_before_bulk_sync(self):
        bridge = srv_files.Files.__new__(srv_files.Files)
        bridge.folder = Path("example")
        bridge.remote_meta = "/home/ai-agent/.local/share/srv/test"
        bridge.info = {"measured_bytes_per_second": 12_500_000}
        bridge.args = argparse.Namespace(allow_large=False)
        bridge.rc = lambda *a, **k: json.dumps({"bytes": 2_000_000_000, "count": 10})
        bridge.ssh = lambda command: "10000000000\n"
        with patch("srv_files.sys_stdin_tty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Большая передача"):
                bridge.estimate()

    def test_first_sync_wont_resync_over_existing_remote_data(self):
        bridge = srv_files.Files.__new__(srv_files.Files)
        bridge.info = {}
        bridge.remote_sync = "/home/ai-agent/workspaces/sync/example"
        calls = []
        def rc(*args, **kwargs):
            calls.append(args)
            return "existing-file.txt\n"
        bridge.rc = rc
        with self.assertRaisesRegex(RuntimeError, "пустого"):
            bridge.sync()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "lsf")

    def test_sync_preserves_conflicting_versions_and_blocks_deletes(self):
        bridge = srv_files.Files.__new__(srv_files.Files)
        bridge.info = {"initialized": True}
        bridge.remote_sync = "/home/ai-agent/workspaces/sync/example"
        bridge.folder = Path("example")
        bridge.local = Path("state")
        bridge.save = lambda: None
        bridge.preflight = lambda args: None
        calls = []
        bridge.rc = lambda *a, **k: calls.append(a)
        with contextlib.redirect_stdout(io.StringIO()):
            bridge.sync()
        command = calls[0]
        self.assertEqual(command[command.index("--max-delete") + 1], "0")
        self.assertEqual(command[command.index("--conflict-resolve") + 1], "none")
        self.assertNotIn("--resync", command)
        self.assertNotIn("--force", command)

    def test_dry_run_conflict_stops_before_changes(self):
        bridge = srv_files.Files.__new__(srv_files.Files)
        bridge.rclone = Path("rclone")
        bridge.config = Path("config")
        result = SimpleNamespace(returncode=0, stderr=json.dumps({"msg": "WARNING New or changed in both paths - file.txt"}))
        with patch("srv_files.subprocess.run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "Правки с обеих"):
                bridge.preflight(["bisync", "local", "remote"])
        self.assertIn("--dry-run", run.call_args.args[0])

    def test_operational_keys_and_git_metadata_are_excluded(self):
        args = srv_files.filter_args()
        self.assertIn("/.local/**", args)
        self.assertIn("/.git/**", args)
        self.assertIn("/.scratch/**", args)


if __name__ == "__main__":
    unittest.main()
