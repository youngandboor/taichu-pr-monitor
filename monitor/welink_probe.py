"""Preview and send controlled WeLink message-format probes."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import time
from typing import Dict, Optional, Sequence

from .welink import WeLinkCli


DEFAULT_PR_URL = "https://taichu.fun/gitea/SystemAgentDev/TaiChu/pulls/1111"
PROBE_CASES = (
    "single-line",
    "url-last",
    "url-followed-by-text",
    "long-single-line",
    "multi-line",
)


def build_probe_messages(
    timestamp: Optional[str] = None,
    pr_url: str = DEFAULT_PR_URL,
) -> Dict[str, str]:
    stamp = timestamp or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    long_detail = "；".join(f"检查项{i:02d}=失败摘要样例" for i in range(1, 25))
    return {
        "single-line": f"[TaiChu PRbot 格式测试] 单行纯文本；时间 {stamp}",
        "url-last": (
            "[TaiChu PR 1111] 发现问题：taichu/pr-build：消息格式测试；"
            "【Taichu PRbot 自动发送，回复TD退订】；查看 "
            f"{pr_url}"
        ),
        "url-followed-by-text": (
            "[TaiChu PR 1111] 发现问题：taichu/pr-build：消息格式测试；查看 "
            f"{pr_url}；【URL 后仍有文字的对照组】"
        ),
        "long-single-line": (
            f"[TaiChu PRbot 格式测试] 较长单行；{long_detail}；查看 {pr_url}"
        ),
        "multi-line": "\n".join(
            (
                "[TaiChu PRbot 格式测试] 多行对照组",
                "第二行：用于确认 WeLink CLI 或客户端是否截断多行消息",
                f"查看 {pr_url}",
            )
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preview or send controlled WeLink message-format probes."
    )
    parser.add_argument(
        "--case",
        choices=(*PROBE_CASES, "all"),
        default="url-last",
        dest="test_case",
        help="message shape to test; all sends every case",
    )
    parser.add_argument("--receiver", default="", help="consenting receiver W3 account")
    parser.add_argument(
        "--send",
        action="store_true",
        help="actually call welink-cli; without this flag the command only previews",
    )
    parser.add_argument(
        "--welink-cli",
        default=(
            os.environ.get("WELINK_CLI")
            or os.environ.get("TAICHU_WELINK_CLI")
            or "welink-cli"
        ),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--pause-seconds", type=float, default=2.0)
    parser.add_argument("--pr-url", default=DEFAULT_PR_URL)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    receiver = args.receiver.strip()
    if args.send and not receiver:
        build_parser().error("--receiver is required when --send is used")
    if args.send and not re.fullmatch(r"[A-Za-z]\d{8}", receiver):
        build_parser().error("--receiver must be a W3 account such as y00000001")
    sender_account = os.environ.get("TAICHU_WELINK_SENDER", "").strip()
    if args.send and sender_account and receiver.casefold() == sender_account.casefold():
        build_parser().error("--receiver must differ from TAICHU_WELINK_SENDER")

    messages = build_probe_messages(pr_url=args.pr_url)
    selected = PROBE_CASES if args.test_case == "all" else (args.test_case,)
    sender = WeLinkCli([args.welink_cli], timeout_seconds=max(1.0, args.timeout))
    failed = False
    for index, name in enumerate(selected):
        message = messages[name]
        _print_probe(name, message, args.pr_url)
        if not args.send:
            print("send=disabled (add --send and --receiver to deliver)\n")
            continue
        result = sender.send(receiver, message)
        print(
            f"send_status={result.status} exit_code={result.exit_code} "
            f"duration_seconds={result.duration_seconds:.3f}"
        )
        if result.stdout.strip():
            print(f"stdout={result.stdout.strip()[:500]}")
        if result.stderr.strip():
            print(f"stderr={result.stderr.strip()[:500]}")
        print()
        failed = failed or result.status != "success"
        if index + 1 < len(selected) and args.pause_seconds > 0:
            time.sleep(args.pause_seconds)
    return 1 if failed else 0


def _print_probe(name: str, message: str, pr_url: str) -> None:
    line_count = len(message.splitlines()) or 1
    print(f"=== {name} ===")
    print(
        f"characters={len(message)} utf8_bytes={len(message.encode('utf-8'))} "
        f"lines={line_count} url_is_last={'yes' if message.endswith(pr_url) else 'no'}"
    )
    print("----- payload -----")
    print(message)
    print("----- end payload -----")


if __name__ == "__main__":
    raise SystemExit(main())
