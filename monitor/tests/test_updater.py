import pathlib
import subprocess
import unittest

from monitor.updater import RepositoryUpdater


class SequenceRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return subprocess.CompletedProcess(command, *response)


class RepositoryUpdaterTest(unittest.TestCase):
    def test_fast_forwards_clean_main_checkout(self):
        runner = SequenceRunner(
            [
                (0, "true\n", ""),
                (0, "main\n", ""),
                (0, "", ""),
                (0, "old\n", ""),
                (0, "", ""),
                (0, "new\n", ""),
                (0, "", ""),
                (0, "", ""),
                (0, "new\n", ""),
            ]
        )
        result = RepositoryUpdater(pathlib.Path("."), runner=runner).update()

        self.assertEqual("updated", result.status)
        self.assertEqual("old", result.before_sha)
        self.assertEqual("new", result.after_sha)
        self.assertIn(["git", "merge", "--ff-only", "origin/main"], runner.calls)

    def test_refuses_dirty_checkout_before_fetching(self):
        runner = SequenceRunner(
            [
                (0, "true\n", ""),
                (0, "main\n", ""),
                (0, " M monitor/service.py\n", ""),
            ]
        )
        result = RepositoryUpdater(pathlib.Path("."), runner=runner).update()

        self.assertEqual("failed", result.status)
        self.assertIn("本地代码改动", result.message)
        self.assertFalse(any(call[1] == "fetch" for call in runner.calls))

    def test_refuses_non_main_branch(self):
        runner = SequenceRunner(
            [
                (0, "true\n", ""),
                (0, "feature\n", ""),
            ]
        )
        result = RepositoryUpdater(pathlib.Path("."), runner=runner).update()

        self.assertEqual("failed", result.status)
        self.assertIn("feature", result.message)

    def test_refuses_diverged_main(self):
        runner = SequenceRunner(
            [
                (0, "true\n", ""),
                (0, "main\n", ""),
                (0, "", ""),
                (0, "old\n", ""),
                (0, "", ""),
                (0, "new\n", ""),
                (1, "", ""),
            ]
        )
        result = RepositoryUpdater(pathlib.Path("."), runner=runner).update()

        self.assertEqual("failed", result.status)
        self.assertIn("分叉", result.message)

    def test_reports_git_failure_without_throwing(self):
        runner = SequenceRunner([OSError("git missing")])

        result = RepositoryUpdater(pathlib.Path("."), runner=runner).update()

        self.assertEqual("failed", result.status)
        self.assertIn("git missing", result.message)


if __name__ == "__main__":
    unittest.main()
