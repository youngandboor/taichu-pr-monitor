import unittest
from unittest import mock

from monitor.__main__ import _restart_monitor, build_parser, main


class MainArgumentsTest(unittest.TestCase):
    def test_gitea_timeout_and_retries_are_configurable(self):
        args = build_parser().parse_args(
            ["--gitea-timeout", "90", "--gitea-retries", "3"]
        )

        self.assertEqual(90, args.gitea_timeout)
        self.assertEqual(3, args.gitea_retries)

    def test_welink_self_recipient_fallback_is_configurable(self):
        args = build_parser().parse_args(
            [
                "--welink-sender",
                "y00000001",
                "--self-fallback-receiver",
                "y00000002",
            ]
        )

        self.assertEqual("y00000001", args.welink_sender)
        self.assertEqual("y00000002", args.self_fallback_receiver)

    def test_strict_recipients_alias_disables_raw_login_fallback(self):
        args = build_parser().parse_args(["--strict-recipients"])

        self.assertTrue(args.require_recipient_map)

    def test_remote_dashboard_actions_require_explicit_flag(self):
        default = build_parser().parse_args([])
        enabled = build_parser().parse_args(["--allow-remote-dashboard-actions"])

        self.assertFalse(default.allow_remote_dashboard_actions)
        self.assertTrue(enabled.allow_remote_dashboard_actions)

    def test_remote_dashboard_actions_require_access_token(self):
        with mock.patch.dict("os.environ", {"TAICHU_DASHBOARD_TOKEN": ""}):
            result = main(["--allow-remote-dashboard-actions"])

        self.assertEqual(2, result)

    def test_dashboard_token_is_configurable(self):
        args = build_parser().parse_args(["--dashboard-token", "secret-value"])

        self.assertEqual("secret-value", args.dashboard_token)

    def test_restart_does_not_open_a_second_dashboard_tab(self):
        logger = mock.Mock()
        with mock.patch("monitor.__main__.os.execv", side_effect=OSError("blocked")) as execv:
            result = _restart_monitor(
                ["--strict-recipients", "--open-dashboard", "--dashboard-port", "8791"],
                logger,
            )

        self.assertEqual(2, result)
        command = execv.call_args.args[1]
        self.assertNotIn("--open-dashboard", command)
        self.assertIn("--strict-recipients", command)
        self.assertIn("8791", command)


if __name__ == "__main__":
    unittest.main()
