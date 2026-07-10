import unittest

from monitor.gitea import ApiResponse, GiteaClient


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


if __name__ == "__main__":
    unittest.main()
