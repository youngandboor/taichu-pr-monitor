import unittest

from monitor.__main__ import build_parser


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


if __name__ == "__main__":
    unittest.main()
