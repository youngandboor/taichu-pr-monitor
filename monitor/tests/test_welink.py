import json
import base64
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from monitor.welink import WeLinkCli


FAKE_CLI = pathlib.Path(__file__).resolve().parents[1] / "fake_welink_cli.py"


class WeLinkCliTest(unittest.TestCase):
    def sender(self, mode, temp_dir, timeout=1.0):
        log_path = pathlib.Path(temp_dir) / "calls.jsonl"
        env = {
            **os.environ,
            "FAKE_WELINK_MODE": mode,
            "FAKE_WELINK_LOG": str(log_path),
            "FAKE_WELINK_SLEEP_SECONDS": "1",
        }
        return (
            WeLinkCli([sys.executable, str(FAKE_CLI)], timeout_seconds=timeout, env=env),
            log_path,
        )

    def test_success_uses_documented_send_to_user_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sender, log_path = self.sender("success", temp_dir)

            result = sender.send("w00123", "PR #7 build failed")

            self.assertEqual("success", result.status)
            call = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(
                ["im", "send-to-user", "--receiver", "w00123", "--text", "PR #7 build failed"],
                call["argv"],
            )

    def test_nonzero_exit_is_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sender, _ = self.sender("failure", temp_dir)

            result = sender.send("w00123", "boom")

            self.assertEqual("failure", result.status)
            self.assertEqual(23, result.exit_code)

    def test_timeout_is_reported_as_ambiguous_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sender, _ = self.sender("timeout", temp_dir, timeout=0.05)

            result = sender.send("w00123", "boom")

            self.assertEqual("timeout", result.status)
            self.assertIsNone(result.exit_code)

    def test_windows_cmd_shim_uses_powershell_wrapper_and_base64_text(self):
        sender = WeLinkCli([r"C:\\tools\\welink-cli.cmd"], platform="win32")

        def fake_which(value):
            if value == r"C:\\tools\\welink-cli.cmd":
                return value
            if value == "powershell.exe":
                return r"C:\\Windows\\powershell.exe"
            return None

        with mock.patch("monitor.welink.shutil.which", side_effect=fake_which):
            command = sender.build_command("w00123", 'failure & detail "quoted"')

        encoded = command[command.index("-TextBase64") + 1]
        self.assertEqual(
            'failure & detail "quoted"',
            base64.b64decode(encoded).decode("utf-8"),
        )
        self.assertIn("invoke_welink.ps1", " ".join(command))
        self.assertNotIn('failure & detail "quoted"', command)


if __name__ == "__main__":
    unittest.main()
