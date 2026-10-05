from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from .gate import execute, load_config

DEFAULT_CONFIG = "/etc/ivrm-ops-gate/config.json"

EXIT_OK = 0
EXIT_DENIED = 10
EXIT_FAILED = 11
EXIT_BUSY = 12
EXIT_USAGE = 64


def _parse(verb_args: list[str]) -> tuple[str, str, str] | None:
    if len(verb_args) != 3 or verb_args[0] not in {"run", "check"}:
        return None
    return verb_args[0], verb_args[1], verb_args[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ops_gate")
    parser.add_argument("--config", default=os.environ.get("OPS_GATE_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("request", nargs="*", help="run|check <ticket> <signature>")
    args = parser.parse_args(argv)

    request = args.request
    original = os.environ.get("SSH_ORIGINAL_COMMAND")
    if original is not None:
        # Invoked as an SSH forced command: ignore argv, use only what the client sent.
        try:
            request = shlex.split(original)
        except ValueError:
            request = []
    parsed = _parse(request)
    if parsed is None:
        print(json.dumps({"status": "denied", "reason": "usage"}))
        return EXIT_USAGE

    verb, ticket, signature = parsed
    try:
        config = load_config(Path(args.config))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "reason": "gate_misconfigured"}))
        print(f"ops_gate config error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    result = execute(config, ticket, signature, dry_run=verb == "check")
    print(
        json.dumps(
            {"status": result.status, "reason": result.reason, "exit_code": result.exit_code, "output": result.output},
            ensure_ascii=False,
        )
    )
    if result.status == "executed":
        return EXIT_OK
    if result.status == "denied":
        return EXIT_BUSY if result.busy else EXIT_DENIED
    return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
