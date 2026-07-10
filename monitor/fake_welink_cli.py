#!/usr/bin/env python3
"""Deterministic fake for local tests; never connects to WeLink."""

import json
import os
import pathlib
import sys
import time


def main() -> int:
    log_path = os.environ.get("FAKE_WELINK_LOG")
    if log_path:
        path = pathlib.Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"argv": sys.argv[1:]}, ensure_ascii=False) + "\n")

    expected_prefix = ["im", "send-to-user", "--receiver"]
    if sys.argv[1:4] != expected_prefix or "--text" not in sys.argv[4:]:
        print("unexpected welink-cli arguments", file=sys.stderr)
        return 64

    mode = os.environ.get("FAKE_WELINK_MODE", "success").lower()
    if mode == "timeout":
        time.sleep(float(os.environ.get("FAKE_WELINK_SLEEP_SECONDS", "5")))
        return 0
    if mode == "failure":
        print("simulated send failure", file=sys.stderr)
        return 23
    if mode != "success":
        print(f"unknown FAKE_WELINK_MODE: {mode}", file=sys.stderr)
        return 64
    print("simulated send success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
