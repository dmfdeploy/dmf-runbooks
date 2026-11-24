#!/usr/bin/env python3
"""Fail unless the ansible-playbook that CI runs uses the interpreter and ansible-core a leg should.

The suites run through `ansible-playbook` on PATH, so this reads `ansible-playbook --version`:
its core version and the Python it runs on. The Python that runs this script does not matter.
The job log gets both lines.
Usage: assert_toolchain.py --python 3.14 --core ansible-core==2.21.6
"""
import argparse
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="expected Python major.minor, for example 3.14")
    parser.add_argument("--core", required=True, help="expected pin, ansible-core==X.Y.Z")
    args = parser.parse_args()

    match = re.fullmatch(r"ansible-core==([0-9]+\.[0-9]+\.[0-9]+)", args.core)
    if not match:
        sys.exit(f"error: --core must look like ansible-core==X.Y.Z, got {args.core!r}")
    pinned = match.group(1)

    try:
        result = subprocess.run(["ansible-playbook", "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        sys.exit("error: ansible-playbook is not on PATH")
    if result.returncode != 0:
        sys.exit(f"error: ansible-playbook --version exited {result.returncode}: {result.stderr.strip()}")
    out = result.stdout
    core = re.search(r"^ansible-playbook \[core ([0-9.]+)\]", out, re.M)
    python = re.search(r"^\s*python version = ([0-9]+\.[0-9]+)\.[0-9]+", out, re.M)
    for line in out.splitlines():
        if line.startswith("ansible-playbook [") or line.strip().startswith(("python version", "executable location")):
            print(line.strip())

    problems = []
    if not core or core.group(1) != pinned:
        problems.append(f"ansible-playbook reports core {core.group(1) if core else None}, the pin is {pinned}")
    if not python or python.group(1) != args.python:
        problems.append(f"ansible-playbook runs on Python {python.group(1) if python else None}, the leg expects {args.python}")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
