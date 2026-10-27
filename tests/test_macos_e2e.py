"""Opt-in real macOS tests; requires RW 'project' and RO 'readonly' grants."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ENABLED = os.environ.get("MACDESK_E2E") == "1"
SHARE = os.environ.get("MACDESK_E2E_SHARE")
CLI = [sys.executable, "-m", "macdesk", "-i", os.environ.get("MACDESK_E2E_INSTANCE", "default")]


@unittest.skipUnless(
    ENABLED and SHARE, "set MACDESK_E2E=1 and MACDESK_E2E_SHARE for real macOS tests"
)
class MacOSTests(unittest.TestCase):
    @classmethod
    def command(cls, *argv, input=b"", check=True, timeout=180):
        result = subprocess.run(
            CLI + list(argv), input=input, capture_output=True, cwd=cls.host, timeout=timeout
        )
        if check and result.returncode:
            raise AssertionError((argv, result.returncode, result.stderr.decode(errors="replace")))
        return result

    @classmethod
    def run_guest(cls, *argv, input=b"", check=True, cwd=None, timeout=180):
        options = ["--cwd", cwd] if cwd else []
        return cls.command("run", *options, "--", *argv, input=input, check=check, timeout=timeout)

    @classmethod
    def setUpClass(cls):
        cls.host = Path(tempfile.mkdtemp(prefix="macdesk-test-", dir=SHARE))
        cls.home = cls.run_guest("/usr/bin/printenv", "HOME", cwd="/").stdout.decode().strip()
        cls.guest = cls.home + "/.local/share/macdesk/work/" + cls.host.name
        cls.readonly = cls.home + "/.local/share/macdesk/mounts/readonly/" + cls.host.name
        cls.run_guest("/bin/mkdir", "-p", cls.guest)
        fixture = Path(__file__).with_name("process.c")
        cls.command("put", str(fixture), cls.guest + "/process.c")
        cls.run_guest(
            "/usr/bin/xcrun",
            "clang",
            "-Wall",
            "-Wextra",
            "-Werror",
            cls.guest + "/process.c",
            "-o",
            cls.guest + "/process",
        )

    @classmethod
    def tearDownClass(cls):
        cls.run_guest("/bin/rm", "-rf", cls.guest, cwd="/")
        shutil.rmtree(cls.host)

    def assert_guest_gone(self, pid):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.run_guest("/bin/kill", "-0", str(pid), check=False)
            if result.returncode == 1:
                return
        self.fail(f"owned guest process {pid} still exists")

    def start_fixture(self, mode, stdin=subprocess.DEVNULL, timeout=0):
        options = ["--timeout", str(timeout)] if timeout else []
        process = subprocess.Popen(
            CLI + ["run", *options, "--", self.guest + "/process", mode],
            cwd=self.host,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        pid = int(process.stdout.readline())
        return process, pid

    def test_live_mount_freshness_ownership_and_readonly(self):
        local = self.host / "space 日本語.txt"
        local.write_bytes(b"host first")
        self.assertEqual(self.run_guest("/bin/cat", local.name).stdout, b"host first")
        local.write_bytes(b"host changed")
        self.assertEqual(self.run_guest("/bin/cat", local.name).stdout, b"host changed")
        self.run_guest("/bin/sh", "-c", 'printf guest > "$1"', "sh", "guest-created")
        self.assertEqual((self.host / "guest-created").read_bytes(), b"guest")
        self.assertEqual((self.host / "guest-created").stat().st_uid, os.getuid())
        self.run_guest("/bin/mv", "guest-created", "renamed")
        self.assertTrue((self.host / "renamed").exists())
        self.run_guest("/bin/rm", "renamed")
        self.assertFalse((self.host / "renamed").exists())
        result = self.run_guest(
            "/usr/bin/sudo", "-n", "/usr/bin/touch", self.readonly + "/denied", check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.host / "denied").exists())

    def test_literal_arguments_binary_stdin_exit_and_environment(self):
        literal = "$(touch SHOULD_NOT_EXIST); ' 日本語"
        self.assertEqual(self.run_guest("/usr/bin/printf", "%s", literal).stdout, literal.encode())
        self.assertFalse((self.host / "SHOULD_NOT_EXIST").exists())
        data = bytes(range(256)) * 16384
        self.assertEqual(self.run_guest("/bin/cat", input=data).stdout, data)
        self.assertEqual(self.run_guest("/bin/sh", "-c", "exit 7", check=False).returncode, 7)
        result = self.command(
            "run", "--env", "EXPLICIT=space 日本語", "--", "/usr/bin/printenv", "EXPLICIT"
        )
        self.assertEqual(result.stdout, "space 日本語\n".encode())

    def test_short_command_exits_while_stdin_remains_open(self):
        process = subprocess.Popen(
            CLI + ["run", "--", "/usr/bin/printf", "%s", "finished"],
            cwd=self.host,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            process.wait(timeout=30)
            self.assertEqual(process.stdout.read(), b"finished")
            self.assertEqual(process.returncode, 0, process.stderr.read().decode(errors="replace"))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for pipe in (process.stdin, process.stdout, process.stderr):
                pipe.close()

    def test_verified_transfers_and_destination_preservation(self):
        source = self.host / "input 日本語.bin"
        source.write_bytes(bytes(range(256)) * 65536)
        destination = self.guest + "/copied 日本語.bin"
        receipt = json.loads(self.command("put", str(source), destination).stdout)
        self.assertEqual(receipt["size"], source.stat().st_size)
        self.assertEqual(receipt["mode"], source.stat().st_mode & 0o777)
        exported = self.host / "exported 日本語.bin"
        self.command("get", destination, str(exported))
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(exported.read_bytes()).digest(),
        )
        source.write_bytes(b"replacement")
        self.assertNotEqual(
            self.command("put", str(source), destination, check=False).returncode, 0
        )
        self.assertNotEqual(
            self.command("get", destination, str(exported), check=False).returncode, 0
        )
        self.command("put", str(source), destination, "--replace")
        self.command("get", destination, str(exported), "--replace")
        self.assertEqual(exported.read_bytes(), b"replacement")

    def test_timeout_and_interrupt_cancel_owned_descendants(self):
        process, pid = self.start_fixture("tree", timeout=2)
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 124)
        self.assert_guest_gone(pid)
        process, pid = self.start_fixture("tree")
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 130)
        self.assert_guest_gone(pid)

    def test_cancel_with_stdin_backpressure(self):
        process, pid = self.start_fixture("sleep", stdin=subprocess.PIPE)

        def producer():
            try:
                process.stdin.write(bytes(4 * 1024**2))
                process.stdin.flush()
            except BrokenPipeError:
                pass

        worker = threading.Thread(target=producer, daemon=True)
        worker.start()
        time.sleep(1)
        process.send_signal(signal.SIGINT)
        process.wait(timeout=30)
        self.assertEqual(process.returncode, 130)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        self.assert_guest_gone(pid)

    def test_cancel_with_stdout_backpressure(self):
        process, pid = self.start_fixture("output")
        time.sleep(3)
        process.send_signal(signal.SIGINT)
        # Verify cancellation over independent control before draining blocked stdout.
        self.assert_guest_gone(pid)
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 130)

    def test_disconnect_cleans_owned_descendants(self):
        process, pid = self.start_fixture("tree")
        process.kill()
        process.communicate(timeout=10)
        self.assert_guest_gone(pid)

    def test_cancel_upload_preserves_destination_and_control_stays_responsive(self):
        source = self.host / "large.bin"
        small = self.host / "original.txt"
        small.write_bytes(b"keep existing destination")
        destination = self.guest + "/atomic.bin"
        self.command("put", str(small), destination)
        with source.open("wb") as output:
            for _ in range(128):
                output.write(bytes(1024**2))
        process = subprocess.Popen(
            CLI + ["put", str(source), destination, "--replace"],
            cwd=self.host,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIn(b"bytes", process.stderr.readline())
        before = time.monotonic()
        self.assertEqual(json.loads(self.command("status").stdout)["stage"], "ready")
        self.assertLess(time.monotonic() - before, 3)
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 130)
        exported = self.host / "preserved.txt"
        self.command("get", destination, str(exported))
        self.assertEqual(exported.read_bytes(), small.read_bytes())


if __name__ == "__main__":
    unittest.main()
