#!/usr/bin/env python3
"""CLI companion to start_stubs.py: stop the detached stub server process.

Reads the pidfile written by start_stubs.py, sends SIGTERM to the recorded
pid, waits briefly for it to exit, and removes the pidfile.

Usage:

    python3 tests/harness/stop_stubs.py --pidfile /tmp/l3-harness.json
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pidfile", default="/tmp/l3-run-guard-harness.json")
    args = parser.parse_args()

    if not os.path.exists(args.pidfile):
        print(f"stop_stubs.py: no pidfile at {args.pidfile} — nothing to stop", file=sys.stderr)
        return 0

    with open(args.pidfile) as f:
        record = json.load(f)

    pid = record.get("pid")
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            for _ in range(100):  # up to ~5s
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    os.remove(args.pidfile)
    print(f"stop_stubs.py: stopped pid {pid}, removed {args.pidfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
