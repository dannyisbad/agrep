#!/usr/bin/env python3
"""Require a successful default-branch CI run for an exact release commit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
SHA = re.compile(r"[0-9a-f]{40}")


class InvalidCIProof(ValueError):
    """The Actions response does not prove the release commit passed CI."""


def validate(payload: object, *, sha: str, branch: str) -> dict:
    if SHA.fullmatch(sha) is None:
        raise InvalidCIProof("release SHA must be 40 lowercase hexadecimal characters")
    if not branch or "\x00" in branch:
        raise InvalidCIProof("default branch is missing or invalid")
    if not isinstance(payload, dict):
        raise InvalidCIProof("Actions response must be an object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise InvalidCIProof("Actions response has no workflow_runs list")

    matches = [
        run for run in runs
        if isinstance(run, dict)
        and run.get("path") == CI_WORKFLOW_PATH
        and run.get("event") == "push"
        and run.get("head_branch") == branch
        and run.get("head_sha") == sha
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    ]
    if not matches:
        raise InvalidCIProof(
            f"{sha} has no successful push-triggered {CI_WORKFLOW_PATH} "
            f"run on default branch {branch!r}"
        )
    valid = [
        run for run in matches
        if type(run.get("run_attempt")) is int and run["run_attempt"] > 0
        and type(run.get("id")) is int and run["id"] > 0
    ]
    if not valid:
        raise InvalidCIProof("matching Actions runs have invalid identities")
    return max(valid, key=lambda run: (run["run_attempt"], run["id"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.response.read_text(encoding="utf-8"))
        run = validate(payload, sha=args.sha, branch=args.branch)
    except (InvalidCIProof, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"release CI proof failed: {exc}", file=sys.stderr)
        return 1
    print(f"release CI proof verified: run {run.get('id')} attempt "
          f"{run.get('run_attempt')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
