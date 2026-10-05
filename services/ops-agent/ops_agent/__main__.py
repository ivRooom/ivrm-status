from __future__ import annotations

import argparse
import json
import logging
import sys

from .agent import AnalysisFailed, analyze, build_request, needs_analysis
from .budget import BudgetExceeded, BudgetLedger
from .config import Settings
from .status_client import StatusClient, StatusFetchError

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _fail(code: str, detail: str, exit_code: int) -> int:
    print(json.dumps({"error": code, "detail": detail}, ensure_ascii=False), file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ops_agent", description="Read-only status analysis (Phase 0)")
    parser.add_argument("--force", action="store_true", help="analyze even if every service is operational")
    parser.add_argument("--dry-run", action="store_true", help="print the request instead of calling Bedrock")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage

    settings = Settings.from_env()
    try:
        snapshot = StatusClient(settings.status_api_base, settings.request_timeout_seconds).snapshot()
    except (StatusFetchError, ValueError) as exc:
        return _fail("status_unavailable", str(exc), 3)

    if args.dry_run:
        print(json.dumps(build_request(settings, snapshot), ensure_ascii=False, indent=2))
        return 0

    if not args.force and not needs_analysis(snapshot["status"]):
        # Healthy: nothing to analyze, so no AWS client, credentials or spend are needed.
        print(json.dumps({"skipped": True, "reason": "all services operational", "cost_jpy": 0, "analysis": None}, ensure_ascii=False, indent=2))
        return 0

    import boto3  # imported late: --dry-run and the healthy path need no AWS setup
    from botocore.config import Config

    try:
        bedrock = boto3.client(
            "bedrock-runtime",
            region_name=settings.bedrock_region,
            config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 2, "mode": "standard"}),
        )
    except Exception as exc:  # noqa: BLE001 - e.g. missing credentials; report only the type
        return _fail("analysis_failed", f"bedrock client setup failed: {type(exc).__name__}", 3)
    try:
        outcome = analyze(settings, bedrock, snapshot, BudgetLedger(settings), force=args.force)
    except BudgetExceeded as exc:
        return _fail("budget_exceeded", str(exc), 2)
    except AnalysisFailed as exc:
        return _fail("analysis_failed", str(exc), 3)

    print(
        json.dumps(
            {
                "skipped": outcome.skipped,
                "reason": outcome.reason,
                "cost_jpy": round(outcome.cost_jpy, 3),
                "analysis": outcome.analysis.to_dict() if outcome.analysis else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
