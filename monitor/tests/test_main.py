import unittest

from monitor.__main__ import build_parser


class MainArgumentsTest(unittest.TestCase):
    def test_gitea_timeout_and_retries_are_configurable(self):
        args = build_parser().parse_args(
            ["--gitea-timeout", "90", "--gitea-retries", "3"]
        )

        self.assertEqual(90, args.gitea_timeout)
        self.assertEqual(3, args.gitea_retries)


if __name__ == "__main__":
    unittest.main()
