import argparse
import base64
import contextlib
import io
from pathlib import Path
import shlex
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import srv


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def launcher(self, *extra):
        args = srv.parser().parse_args([
            "keygen", "--host", "192.0.2.1", "--identity", str(self.root / "identity"),
            "--state-dir", str(self.root / "state"), *extra,
        ])
        return srv.Launcher(args)

    def test_port_propagates_and_password_fallback_is_disabled(self):
        launcher = self.launcher("--port", "2222")
        for user in ("root", "agent"):
            command = launcher.ssh_args(user, tty=True)
            self.assertEqual(command[command.index("-p") + 1], "2222")
            self.assertIn("PasswordAuthentication=no", command)
            self.assertIn("IdentitiesOnly=yes", command)
            self.assertIn("StrictHostKeyChecking=ask", command)
        self.assertIn("NumberOfPasswordPrompts=1", launcher.ssh_args("root", password=True))

    def test_server_guide_uses_profile_and_preserves_private_notes(self):
        launcher = self.launcher("--port", "2222", "--user", "developer", "--agent", "claude",
                                 "--folder", str(self.root / "team's files"))
        launcher.event("bootstrap", "ok")
        with patch.object(srv, "ROOT", self.root):
            guide = launcher.write_server_guide()
            credentials = guide.parent / "192.0.2.1_2222-developer.credentials.md"
            credentials.write_text("private notes stay here", encoding="utf-8")
            launcher.write_server_guide()
        text = guide.read_text(encoding="utf-8")
        self.assertIn(str(launcher.identity), text)
        self.assertIn(str(launcher.state_dir), text)
        self.assertIn("developer@192.0.2.1", text)
        self.assertIn("claude", text)
        self.assertIn("2222", text)
        self.assertIn("bootstrap: ok", text)
        self.assertNotIn("private notes stay here", text)
        self.assertEqual(credentials.read_text(), "private notes stay here")
        if srv.os.name == "nt":
            self.assertIn("team''s files", text)
        else:
            self.assertEqual(guide.stat().st_mode & 0o777, 0o600)
            self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)

    def test_server_guides_are_separate_for_hosts_and_ports(self):
        first = self.launcher()
        second = self.launcher("--port", "2222")
        args = srv.parser().parse_args(["guide", "--host", "example.invalid", "--state-dir", str(self.root / "state")])
        third = srv.Launcher(args)
        with patch.object(srv, "ROOT", self.root):
            paths = [item.write_server_guide() for item in (first, second, third)]
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all(path.is_file() for path in paths))

    def test_guide_action_needs_no_ssh_and_keeps_local_data_out_of_state(self):
        with patch.object(srv, "ROOT", self.root), patch.object(srv.sys, "argv", [
            "srv.py", "guide", "--host", "example.invalid", "--port", "2222",
            "--state-dir", str(self.root / "custom-state"),
        ]), patch.object(srv.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(srv.main(), 0)
        run.assert_not_called()
        paths = list((self.root / ".local/server-guides").glob("*.md"))
        self.assertEqual(len(paths), 2)
        guide = next(path for path in paths if not path.name.endswith(".credentials.md"))
        self.assertIn("guide: ok", guide.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "custom-state/server-guides").exists())

    def test_failed_setup_still_updates_server_guide(self):
        with patch.object(srv, "ROOT", self.root), patch.object(srv.sys, "argv", [
            "srv.py", "setup", "--host", "example.invalid", "--state-dir", str(self.root / "state"),
        ]), patch.object(srv.Launcher, "local_requirements", side_effect=RuntimeError("missing ssh")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(srv.main(), 1)
        guide = self.root / ".local/server-guides/example.invalid_22-ai-agent-codex.md"
        self.assertIn("setup: failed", guide.read_text(encoding="utf-8"))

    def test_untrusted_arguments_rejected(self):
        for host in ("-oProxyCommand=bad", "x;id", "x\nroot", "root@host"):
            with self.assertRaises(argparse.ArgumentTypeError):
                srv.host_name(host)
        for port in ("0", "65536", "bad"):
            with self.assertRaises(argparse.ArgumentTypeError):
                srv.port_number(port)
        for user in ("root", "a;id", "UPPER"):
            with self.assertRaises(argparse.ArgumentTypeError):
                srv.user_name(user)

    def test_remote_payload_is_exact_and_shell_safe(self):
        script = "printf '%s' \"$(id)\"\n# `a` \\\n+кириллица\n"
        command = srv.remote_command(script)
        tokens = shlex.split(command)
        self.assertEqual(base64.b64decode(tokens[2]).decode("utf-8"), script)
        self.assertNotIn("$(id)", command)

    def test_half_key_pair_never_overwritten(self):
        launcher = self.launcher()
        launcher.identity.write_text("existing private placeholder")
        with patch("srv.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                launcher.keygen()
        run.assert_not_called()
        self.assertEqual(launcher.identity.read_text(), "existing private placeholder")

    def test_existing_pair_does_not_regenerate(self):
        launcher = self.launcher()
        launcher.identity.write_text("private placeholder")
        Path(str(launcher.identity) + ".pub").write_text("public placeholder")
        with patch.object(launcher, "call") as call, contextlib.redirect_stdout(io.StringIO()):
            launcher.keygen()
        self.assertEqual(call.call_count, 1)
        self.assertIn("-l", call.call_args.args[0])

    def test_conflicting_local_profile_is_rejected(self):
        self.launcher().event("sample", "ok")
        with self.assertRaises(RuntimeError):
            self.launcher("--user", "other")

    def test_parallel_worker_snapshots_preserve_other_workers_steps(self):
        first = self.launcher()
        second = self.launcher()
        first.event("connect", "running")
        second.event("files-add", "running")
        first.event("connect", "ok")
        state = __import__("json").loads(first.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["steps"]["files-add"]["status"], "running")
        self.assertEqual(state["steps"]["connect"]["status"], "ok")

    def test_wireguard_validation_does_not_expose_bad_key(self):
        value = "not-a-private-key-secret"
        config = "[Interface]\nPrivateKey=" + value + "\n[Peer]\nPublicKey=" + base64.b64encode(bytes(32)).decode()
        with self.assertRaises(RuntimeError) as error:
            srv.validate_wireguard(config)
        self.assertNotIn(value, str(error.exception))

    def test_wireguard_accepts_multiple_peers_for_staging(self):
        key = base64.b64encode(bytes(32)).decode()
        srv.validate_wireguard("[Interface]\nPrivateKey=" + key + "\n[Peer]\nPublicKey=" + key + "\n[Peer]\nPublicKey=" + key)

    def test_state_has_metadata_but_no_console_capture(self):
        launcher = self.launcher()
        with patch("srv.subprocess.run") as run:
            run.return_value.returncode = 255
            with self.assertRaises(RuntimeError):
                launcher.call(["ssh", "host"], "password_attempt")
        self.assertEqual(launcher.state["steps"]["password_attempt"]["status"], "exit_255")
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("capture_output", run.call_args.kwargs)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_public_key_must_be_single_ed25519_line(self):
        launcher = self.launcher()
        public = Path(str(launcher.identity) + ".pub")
        public.write_text("ssh-ed25519 AAAA comment\nmalicious second line")
        with self.assertRaises(RuntimeError):
            launcher.public_key()

    def test_wrong_host_fingerprint_never_modifies_known_hosts(self):
        launcher = self.launcher("--trust-host-fingerprint", "SHA256:" + "A" * 43)
        known = self.root / ".ssh" / "known_hosts"
        known.parent.mkdir()
        known.write_text("other-host ssh-ed25519 untouched\n")
        replies = [SimpleNamespace(stdout=b"192.0.2.1 ssh-ed25519 AAAA\n", returncode=0),
                   SimpleNamespace(stdout=("256 SHA256:" + "B" * 43 + " host\n").encode(), returncode=0)]
        with patch("srv.Path.home", return_value=self.root), patch("srv.subprocess.run", side_effect=replies):
            with self.assertRaises(RuntimeError):
                launcher.trust_host()
        self.assertEqual(known.read_text(), "other-host ssh-ed25519 untouched\n")
        self.assertFalse(list(known.parent.glob("*backup*")))

    def test_accepted_reinstall_targets_port_and_invalidates_old_progress(self):
        fingerprint = "SHA256:" + "A" * 43
        launcher = self.launcher("--port", "2222", "--trust-host-fingerprint", fingerprint)
        launcher.event("root_key_verified", "ok")
        known = self.root / ".ssh" / "known_hosts"
        known.parent.mkdir()
        known.write_text("other-host ssh-ed25519 untouched\n")
        replies = [SimpleNamespace(stdout=b"[192.0.2.1]:2222 ssh-ed25519 AAAA\n", returncode=0),
                   SimpleNamespace(stdout=f"256 {fingerprint} host\n".encode(), returncode=0),
                   SimpleNamespace(stdout=b"", returncode=1)]
        with patch("srv.Path.home", return_value=self.root), patch("srv.subprocess.run", side_effect=replies), \
             patch.object(launcher, "call") as call, contextlib.redirect_stdout(io.StringIO()):
            launcher.trust_host()
        self.assertEqual(call.call_args.args[0][2], "[192.0.2.1]:2222")
        self.assertIn("other-host ssh-ed25519 untouched", known.read_text())
        self.assertIn("[192.0.2.1]:2222 ssh-ed25519 AAAA", known.read_text())
        self.assertEqual(len(list(known.parent.glob("*backup*"))), 1)
        self.assertNotIn("root_key_verified", launcher.state["steps"])

    def test_fallback_probe_never_sends_credentials_or_changes_main_store(self):
        launcher = self.launcher("--trust-host-fingerprint", "SHA256:" + "A" * 43)
        def fake_run(command, **kwargs):
            if command[0] == "ssh":
                self.assertIn("PreferredAuthentications=none", command)
                self.assertIn("PubkeyAuthentication=no", command)
                self.assertIn("PasswordAuthentication=no", command)
                self.assertIn("KbdInteractiveAuthentication=no", command)
                probe = next(value.split("=", 1)[1] for value in command if value.startswith("UserKnownHostsFile="))
                self.assertNotEqual(Path(probe), self.root / ".ssh" / "known_hosts")
                Path(probe).write_text("192.0.2.1 ssh-ed25519 AAAA\n", encoding="ascii")
                return SimpleNamespace(returncode=255)
            return SimpleNamespace(stdout=("256 SHA256:" + "B" * 43 + " host\n").encode(), returncode=0)
        with patch("srv.shutil.which", return_value=None), patch("srv.subprocess.run", side_effect=fake_run), \
             patch("srv.Path.home", return_value=self.root), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                launcher.trust_host()
        self.assertFalse((self.root / ".ssh" / "known_hosts").exists())


if __name__ == "__main__":
    unittest.main()
