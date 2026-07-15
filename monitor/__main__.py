"""Command-line entry point for ``python -m monitor``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
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
from .instance_lock import InstanceAlreadyRunning, InstanceLock
from .service import MonitorService, RecipientDirectory
from .state import MonitorStore
from .updater import RepositoryUpdater
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
    parser.add_argument(
        "--gitea-timeout",
        type=positive_float,
        default=60,
        help="timeout for each Gitea API request in seconds (default: 60)",
    )
    parser.add_argument(
        "--gitea-retries",
        type=non_negative_int,
        default=2,
        help="retries after a Gitea network error (default: 2)",
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
        "--strict-recipients",
        action="store_true",
        help="require a Gitea-derived W3 or override; disable raw-login fallback",
    )
    parser.add_argument(
        "--welink-cli",
        default=os.environ.get("WELINK_CLI", "welink-cli"),
        help="welink-cli executable or Windows npm .cmd shim",
    )
    parser.add_argument(
        "--welink-sender",
        default=os.environ.get("TAICHU_WELINK_SENDER", ""),
        help="W3 account logged into welink-cli, used to detect unsupported self-messages",
    )
    parser.add_argument(
        "--self-fallback-receiver",
        default=os.environ.get("TAICHU_WELINK_SELF_FALLBACK", ""),
        help="alternate W3 receiver when the intended recipient equals the WeLink sender",
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
        "--allow-remote-dashboard-actions",
        action="store_true",
        help="allow dashboard management actions from non-loopback clients",
    )
    parser.add_argument(
        "--dashboard-token",
        default=os.environ.get("TAICHU_DASHBOARD_TOKEN", ""),
        help="HTTP Basic password used to protect the dashboard",
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
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_args)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("taichu-pr-monitor")
    if args.list_outbox:
        with MonitorStore(args.state_db) as store:
            print(
                json.dumps(
                    [record.__dict__ for record in store.list_outbox()],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0

    dashboard_enabled = not args.once and not args.no_dashboard
    if (
        dashboard_enabled
        and args.allow_remote_dashboard_actions
        and not args.dashboard_token
    ):
        logger.error(
            "--allow-remote-dashboard-actions requires --dashboard-token or "
            "TAICHU_DASHBOARD_TOKEN"
        )
        return 2

    instance_lock = InstanceLock(args.state_db)
    try:
        instance_lock.acquire()
    except InstanceAlreadyRunning as error:
        logger.error("%s", error)
        return 2

    store = None
    dashboard = None
    restart_after_update = False
    wake_event = threading.Event()
    runtime = DashboardRuntime(wake_event)
    try:
        store = MonitorStore(args.state_db)

        credential = resolve_credential(args.web_base, args.owner, args.repo)
        if credential is None:
            logger.warning(
                "no Gitea credential found; requests will be anonymous unless the repository allows it"
            )
        client = GiteaClient(
            args.api_base,
            credential,
            timeout_seconds=args.gitea_timeout,
            max_retries=args.gitea_retries,
        )
        sender = (
            DryRunSender()
            if args.dry_run
            else WeLinkCli([args.welink_cli], timeout_seconds=args.welink_timeout)
        )
        try:
            recipients = RecipientDirectory(
                path=args.recipients,
                direct=not args.require_recipient_map,
                sender_account=args.welink_sender,
                self_fallback_receiver=args.self_fallback_receiver,
            )
        except ValueError as error:
            logger.error("recipient configuration error: %s", error)
            return 2
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

        if dashboard_enabled:
            dashboard = DashboardServer(
                host=args.dashboard_host,
                port=args.dashboard_port,
                state_path=args.state_db,
                runtime=runtime,
                logger=logger,
                allow_remote_actions=args.allow_remote_dashboard_actions,
                access_token=args.dashboard_token,
            )
            dashboard.start()
            logger.info("dashboard available at %s", dashboard.url)
            if args.dashboard_host not in {"127.0.0.1", "localhost", "::1"}:
                if args.dashboard_token:
                    logger.warning(
                        "dashboard is remotely reachable with HTTP Basic authentication; "
                        "bind it only to a trusted network"
                    )
                else:
                    logger.warning(
                        "dashboard has no authentication; bind it only to a trusted network"
                    )
            if args.allow_remote_dashboard_actions:
                logger.warning(
                    "remote dashboard management actions are enabled with HTTP Basic authentication"
                )
            if args.open_dashboard:
                webbrowser.open(dashboard.url)

        while True:
            if runtime.claim_update():
                result = RepositoryUpdater(
                    pathlib.Path(__file__).resolve().parents[1]
                ).update()
                runtime.finish_update(
                    result.status,
                    result.message,
                    result.before_sha,
                    result.after_sha,
                )
                if result.status == "updated":
                    logger.info("%s", result.message)
                    restart_after_update = True
                    break
                if result.status == "failed":
                    logger.error("%s", result.message)
                else:
                    logger.info("%s", result.message)

            if runtime.is_paused():
                wake_event.wait()
                wake_event.clear()
                continue

            cycle_started = time.monotonic()
            if not runtime.scan_started():
                continue
            try:
                report = service.poll_once()
            finally:
                runtime.scan_finished()
            logger.info(
                "poll complete: open=%s scanned=%s new_notifications=%s sent=%s "
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
        if store is not None:
            store.close()
        instance_lock.release()

    if restart_after_update:
        return _restart_monitor(raw_args, logger)
    return 0


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


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _environment_path(name: str):
    value = os.environ.get(name)
    return pathlib.Path(value) if value else None


def _restart_monitor(arguments, logger: logging.Logger) -> int:
    restart_arguments = [argument for argument in arguments if argument != "--open-dashboard"]
    command = [sys.executable, "-m", "monitor", *restart_arguments]
    try:
        os.execv(sys.executable, command)
    except OSError as error:
        logger.error("updated successfully but could not restart monitor: %s", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
