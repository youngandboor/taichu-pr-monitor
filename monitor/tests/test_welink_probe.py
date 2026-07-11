import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from monitor.welink import DeliveryResult
from monitor.welink_probe import (
    DEFAULT_PR_URL,
    PROBE_CASES,
    build_parser,
    build_probe_messages,
    main,
)


class WeLinkProbeTest(unittest.TestCase):
    def test_probe_cases_preserve_expected_line_shapes_and_url_boundary(self):
        messages = build_probe_messages("2026-07-11T12:00:00+00:00")

        self.assertEqual(set(PROBE_CASES), set(messages))
        for name in ("single-line", "url-last", "long-single-line"):
            self.assertNotIn("\n", messages[name])
        self.assertGreater(len(messages["multi-line"].splitlines()), 1)
        self.assertTrue(messages["url-last"].endswith(DEFAULT_PR_URL))
        self.assertFalse(messages["url-followed-by-text"].endswith(DEFAULT_PR_URL))
        self.assertIn(DEFAULT_PR_URL, messages["url-followed-by-text"])
        self.assertTrue(messages["long-single-line"].endswith(DEFAULT_PR_URL))
        self.assertTrue(messages["multi-line"].endswith(DEFAULT_PR_URL))

    def test_preview_does_not_call_welink(self):
        output = io.StringIO()
        with mock.patch("monitor.welink_probe.WeLinkCli.send") as send:
            with redirect_stdout(output):
                result = main(["--case", "url-last"])

        self.assertEqual(0, result)
        send.assert_not_called()
        self.assertIn("send=disabled", output.getvalue())

    def test_explicit_send_reports_delivery_failure(self):
        delivery = DeliveryResult("failure", 9, "", "rejected", 0.01)
        with mock.patch("monitor.welink_probe.WeLinkCli.send", return_value=delivery):
            with redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--case",
                        "single-line",
                        "--send",
                        "--receiver",
                        "y00000001",
                    ]
                )

        self.assertEqual(1, result)

    def test_explicit_send_preserves_multiline_payload(self):
        delivery = DeliveryResult("success", 0, "sent", "", 0.01)
        with mock.patch(
            "monitor.welink_probe.WeLinkCli.send", return_value=delivery
        ) as send:
            with redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--case",
                        "multi-line",
                        "--send",
                        "--receiver",
                        "y00000001",
                    ]
                )

        self.assertEqual(0, result)
        receiver, payload = send.call_args.args
        self.assertEqual("y00000001", receiver)
        self.assertEqual(3, len(payload.splitlines()))
        self.assertTrue(payload.endswith(DEFAULT_PR_URL))

    def test_probe_uses_the_same_welink_cli_environment_as_monitor(self):
        with mock.patch.dict(
            "os.environ",
            {
                "WELINK_CLI": "configured-welink-cli",
                "TAICHU_WELINK_CLI": "legacy-welink-cli",
            },
        ):
            args = build_parser().parse_args([])

        self.assertEqual("configured-welink-cli", args.welink_cli)

    def test_send_rejects_invalid_or_self_receiver(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--send", "--receiver", "not-a-w3"])

        with mock.patch.dict(
            "os.environ",
            {"TAICHU_WELINK_SENDER": "y00000001"},
        ):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["--send", "--receiver", "Y00000001"])


if __name__ == "__main__":
    unittest.main()
