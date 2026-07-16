# Gitea PR Brief

Read-only local bridge for a compact TaiChu Gitea PR view.

It shows only:

- latest queue / running clues from PR comments;
- PR body;
- latest non-success signal for these gates:
  - `protected-file-approval`
  - `taichu/codex-pr-review`
  - `taichu/codex-pr-test-review`
  - `taichu/pr-build`
  - `taichu/dev-cloud-preflight`
  - `ci/merge-gate`

Successful gate contexts are intentionally hidden.

```bash
python3 web/gitea_pr_brief.py \
  https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1222 \
  --serve
```

Then open `http://127.0.0.1:8787/pr/1222`.

The same server can show any PR in the configured repo:

```text
http://127.0.0.1:8787/pr/1287
```

You can also switch PRs from the page header without restarting the bridge.

The bridge reads credentials from `TAICHU_GITEA_TOKEN` / `GITEA_TOKEN`,
`TAICHU_GITEA_USERNAME` + `TAICHU_GITEA_PASSWORD`, or `git credential fill`.
Credentials stay in process memory and are not written to disk.
