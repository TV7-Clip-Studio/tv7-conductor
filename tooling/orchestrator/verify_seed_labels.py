#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys


REQUIRED_BASE = {
    "conductor",
    "type:task",
    "source:roadmap",
}


def _run_gh(args: list[str]) -> dict[str, object]:
    cmd = ["gh", *args]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh failed: {' '.join(cmd)}")
    return json.loads(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert required Project Conductor seed labels on an issue.")
    parser.add_argument("--repo", required=True, help="GitHub repo slug, e.g. TV7-Clip-Studio/tv7-core")
    parser.add_argument("--issue", required=True, help="Issue number or URL")
    parser.add_argument("--target-repo", required=True, help="Expected dynamic label repo:<target-repo>")
    parser.add_argument("--batch-id", default="", help="Expected batch id for label batch:<batch-id>")
    args = parser.parse_args(argv)

    payload = _run_gh(["issue", "view", args.issue, "--repo", args.repo, "--json", "labels,url,title"])
    labels_raw = payload.get("labels", [])
    labels = {
        str(entry.get("name", "")).strip().lower()
        for entry in labels_raw
        if isinstance(entry, dict)
    }

    expected = set(REQUIRED_BASE)
    expected.add("stage:seed")
    expected.add(f"repo:{args.target_repo}".lower())
    if args.batch_id:
        expected.add(f"batch:{args.batch_id}".lower())

    missing = sorted(expected - labels)
    if missing:
        print("Label assertion failed.")
        print(f"Issue: {payload.get('url', args.issue)}")
        print("Missing labels:")
        for label in missing:
            print(f"- {label}")
        return 1

    print("Label assertion passed.")
    print(f"Issue: {payload.get('url', args.issue)}")
    print(f"Title: {payload.get('title', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
