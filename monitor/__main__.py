"""Command-line entry point for ``python -m monitor``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import threading
import time
import webbrowser

from .core import DEFAULT_POLL_INTERVAL_SECONDS
from .dashboard import DashboardRuntime, DashboardServer
from .gitea import (
    DEFAULT_API_BASE,
    DEFAULT_OWNER,
    DEFAULT_REPO,
    DEFAULT_WEB_BASE,
    GiteaClient,
    resolve_credential,
)
from .service import MonitorService, RecipientDirectory
from .state import MonitorStore
from .welink import DryRunSender, WeLinkCli


DEFAULT_STATE_DB = pathlib.Path(__file__).resolve().parent / ".state" / "monitor.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor all open SystemAgentDev/TaiChu PRs and notify authors through WeLink.",
    )
    parser.add_argument("--once", action="store_true", help="run one polling cycle and exit")
    parser.add_argument(
        "--poll-interval",
        type=positive_int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="seconds between polling cycles (default: 180)",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--web-base", default=DEFAULT_WEB_BASE)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--state-db",
        type=pathlib.Path,
        default=DEFAULT_STATE_DB,
        help="SQLite state/outbox path",
    )
    parser.add_argument(
        "--recipients",
        type=pathlib.Path,
        default=_environment_path("TAICHU_WELINK_RECIPIENTS_FILE"),
        help="optional JSON overrides from Gitea login to WeLink user",
    )
    parser.add_argument(
        "--require-recipient-map",
        action="store_true",
        help="disable direct Gitea-login-to-WeLink-user mapping",
    )
    parser.add_argument(
        "--welink-cli",
        default=os.environ.get("WELINK_CLI", "welink-cli"),
        help="welink-cli executable or Windows npm .cmd shim",
    )
    parser.add_argument(
        "--welink-timeout",
        type=positive_float,
        default=20.0,
        help="send timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--max-send-attempts",
        type=positive_int,
        default=3,
        help="maximum attempts after nonzero exits (default: 3)",
    )
    parser.add_argument(
        "--fetch-workers",
        type=positive_int,
        default=6,
        help="concurrent Gitea PR fetches (default: 6)",
    )
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="dashboard bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=positive_int,
        default=8790,
        help="dashboard port (default: 8790)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="disable the local operational dashboard",
    )
    parser.add_argument(
        "--open-dashboard",
        action="store_true",
        help="open the dashboard in the default browser at startup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print would-be messages instead of invoking welink-cli",
    )
    parser.add_argument(
        "--list-outbox",
        action="store_true",
        help="print the durable delivery outbox as JSON and exit",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("taichu-pr-monitor")
    store = MonitorStore(args.state_db)
    dashboard = None
    wake_event = threading.Event()
    runtime = DashboardRuntime(wake_event)
    try:
        if args.list_outbox:
            print(
                json.dumps(
                    [record.__dict__ for record in store.list_outbox()],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        credential = resolve_credential(args.web_base, args.owner, args.repo)
        if credential is None:
            logger.warning(
                "no Gitea credential found; requests will be anonymous unless the repository allows it"
            )
        client = GiteaClient(args.api_base, credential)
        sender = (
            DryRunSender()
            if args.dry_run
            else WeLinkCli([args.welink_cli], timeout_seconds=args.welink_timeout)
        )
        recipients = RecipientDirectory(
            path=args.recipients,
            direct=not args.require_recipient_map,
        )
        service = MonitorService(
            client=client,
            store=store,
            sender=sender,
            recipients=recipients,
            owner=args.owner,
            repo=args.repo,
            web_base=args.web_base,
            max_send_attempts=args.max_send_attempts,
            fetch_workers=args.fetch_workers,
            logger=logger,
        )

        if not args.once and not args.no_dashboard:
            dashboard = DashboardServer(
                host=args.dashboard_host,
                port=args.dashboard_port,
                state_path=args.state_db,
                runtime=runtime,
                logger=logger,
            )
            dashboard.start()
            logger.info("dashboard available at %s", dashboard.url)
            if args.dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
                logger.warning("dashboard has no authentication; bind it only to a trusted network")
            if args.open_dashboard:
                webbrowser.open(dashboard.url)

        while True:
            cycle_started = time.monotonic()
            runtime.scan_started()
            try:
                report = service.poll_once()
            finally:
                runtime.scan_finished()
            logger.info(
                "poll complete: open=%s scanned=%s new_failures=%s sent=%s "
                "send_failed=%s uncertain=%s unmapped=%s errors=%s",
                report.open_prs,
                report.scanned_prs,
                report.new_notifications,
                report.delivered,
                report.delivery_failures,
                report.delivery_uncertain,
                report.unmapped,
                len(report.errors),
            )
            if args.once:
                return 1 if report.errors else 0
            elapsed = time.monotonic() - cycle_started
            wake_event.wait(max(1.0, args.poll_interval - elapsed))
            wake_event.clear()
    except KeyboardInterrupt:
        logger.info("monitor stopped")
        return 0
    finally:
        if dashboard is not None:
            dashboard.close()
        store.close()


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _environment_path(name: str):
    value = os.environ.get(name)
    return pathlib.Path(value) if value else None


if __name__ == "__main__":
    raise SystemExit(main())
