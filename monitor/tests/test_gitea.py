import json
import unittest
import urllib.error
from unittest import mock

from monitor.gitea import ApiResponse, GiteaApiError, GiteaClient


class FakeHttpResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


class RecordingClient(GiteaClient):
    def __init__(self, pages, headers=None):
        super().__init__("https://taichu.fun/gitea/api/v1", credential=None)
        self.pages = list(pages)
        self.headers = list(headers or [{} for _ in pages])
        self.paths = []

    def api_get_response(self, path):
        self.paths.append(path)
        return ApiResponse(self.pages.pop(0), self.headers.pop(0))


class GiteaClientTest(unittest.TestCase):
    def test_timeout_is_retried_with_configured_request_timeout(self):
        client = GiteaClient(
            "https://taichu.fun/gitea/api/v1",
            credential=None,
            timeout_seconds=75,
            max_retries=2,
            retry_backoff_seconds=0.25,
        )
        timed_out = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch(
            "monitor.gitea.urllib.request.urlopen",
            side_effect=[timed_out, FakeHttpResponse([{"number": 1}])],
        ) as urlopen, mock.patch("monitor.gitea.time.sleep") as sleep:
            response = client.api_get_response("/repos/SystemAgentDev/TaiChu/pulls")

        self.assertEqual([{"number": 1}], response.payload)
        self.assertEqual(2, urlopen.call_count)
        self.assertTrue(all(call.kwargs["timeout"] == 75 for call in urlopen.call_args_list))
        sleep.assert_called_once_with(0.25)

    def test_timeout_error_reports_attempt_count_after_retries_are_exhausted(self):
        client = GiteaClient(
            "https://taichu.fun/gitea/api/v1",
            credential=None,
            max_retries=2,
            retry_backoff_seconds=0,
        )
        timed_out = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch(
            "monitor.gitea.urllib.request.urlopen",
            side_effect=timed_out,
        ) as urlopen, mock.patch("monitor.gitea.time.sleep"):
            with self.assertRaisesRegex(GiteaApiError, "after 3 attempts"):
                client.api_get_response("/repos/SystemAgentDev/TaiChu/pulls")

        self.assertEqual(3, urlopen.call_count)

    def test_lists_every_open_pull_request_with_pagination(self):
        client = RecordingClient(
            [
                [{"number": 1}, {"number": 2}],
                [{"number": 3}],
            ]
        )

        pulls = client.list_open_pulls("SystemAgentDev", "TaiChu", max_pages=5, limit=2)

        self.assertEqual([1, 2, 3], [item["number"] for item in pulls])
        self.assertEqual(2, len(client.paths))
        self.assertTrue(all("state=open" in path for path in client.paths))
        self.assertTrue(all("sort=recentupdate" in path for path in client.paths))

    def test_get_pull_reads_detail_used_for_merge_metrics(self):
        client = RecordingClient(
            [
                {
                    "number": 7,
                    "created_at": "2026-07-10T00:00:00Z",
                    "additions": 100,
                    "deletions": 20,
                }
            ]
        )

        pull = client.get_pull("SystemAgentDev", "TaiChu", 7)

        self.assertEqual(100, pull["additions"])
        self.assertTrue(client.paths[0].endswith("/repos/SystemAgentDev/TaiChu/pulls/7"))

    def test_get_pull_rejects_an_invalid_detail_response(self):
        client = RecordingClient([[{"number": 7}]])

        with self.assertRaisesRegex(GiteaApiError, "invalid pull request response"):
            client.get_pull("SystemAgentDev", "TaiChu", 7)

    def test_follows_link_header_when_server_caps_requested_page_size(self):
        link = (
            '<https://taichu.fun/gitea/api/v1/repos/x/y/pulls?page=2>; rel="next",'
            '<https://taichu.fun/gitea/api/v1/repos/x/y/pulls?page=3>; rel="last"'
        )
        client = RecordingClient(
            [
                [{"number": 1}, {"number": 2}],
                [{"number": 3}, {"number": 4}],
                [{"number": 5}],
            ],
            [{"Link": link, "X-Total-Count": "5"}, {}, {}],
        )

        pulls = client.list_open_pulls("SystemAgentDev", "TaiChu", limit=100)

        self.assertEqual([1, 2, 3, 4, 5], [item["number"] for item in pulls])
        self.assertEqual(3, len(client.paths))

    def test_recent_pages_skips_old_comment_pages(self):
        link = (
            '<https://taichu.fun/gitea/api/v1/comments?page=2>; rel="next",'
            '<https://taichu.fun/gitea/api/v1/comments?page=3>; rel="last"'
        )
        client = RecordingClient(
            [
                [{"id": 1}, {"id": 2}],
                [{"id": 3}, {"id": 4}],
                [{"id": 5}],
            ],
            [{"Link": link}, {}, {}],
        )

        comments = client.api_get_pages("/comments", max_pages=2, recent=True)

        self.assertEqual([3, 4, 5], [item["id"] for item in comments])
        self.assertIn("page=2", client.paths[1])

    def test_status_endpoint_falls_back_to_combined_status(self):
        client = RecordingClient(
            [
                [],
                {"statuses": [{"context": "taichu/pr-build", "state": "success"}]},
            ]
        )

        statuses = client.get_statuses("SystemAgentDev", "TaiChu", "abc123")

        self.assertEqual("taichu/pr-build", statuses[0]["context"])
        self.assertIn("/statuses/abc123", client.paths[0])
        self.assertIn("/commits/abc123/status", client.paths[1])

    def test_create_issue_comment_posts_ci_merge_once(self):
        client = GiteaClient(
            "https://taichu.fun/gitea/api/v1",
            credential=None,
            timeout_seconds=75,
            max_retries=4,
        )
        with mock.patch(
            "monitor.gitea.urllib.request.urlopen",
            return_value=FakeHttpResponse({"id": 91, "body": "/ci merge"}),
        ) as urlopen:
            comment = client.create_issue_comment(
                "SystemAgentDev",
                "TaiChu",
                7,
                "/ci merge",
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(91, comment["id"])
        self.assertEqual("POST", request.get_method())
        self.assertTrue(request.full_url.endswith("/issues/7/comments"))
        self.assertEqual({"body": "/ci merge"}, json.loads(request.data.decode("utf-8")))
        self.assertEqual("application/json", request.headers["Content-type"])
        self.assertEqual(1, urlopen.call_count)

    def test_create_issue_comment_never_retries_network_failure(self):
        client = GiteaClient(
            "https://taichu.fun/gitea/api/v1",
            credential=None,
            max_retries=5,
            retry_backoff_seconds=0.25,
        )
        timed_out = urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch(
            "monitor.gitea.urllib.request.urlopen",
            side_effect=timed_out,
        ) as urlopen, mock.patch("monitor.gitea.time.sleep") as sleep:
            with self.assertRaisesRegex(GiteaApiError, "request failed"):
                client.create_issue_comment(
                    "SystemAgentDev",
                    "TaiChu",
                    7,
                    "/ci merge",
                )

        self.assertEqual(1, urlopen.call_count)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
