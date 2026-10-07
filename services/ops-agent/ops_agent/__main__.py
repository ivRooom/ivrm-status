from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .agent import AnalysisFailed, analyze, build_request, needs_analysis
from .budget import BudgetExceeded, BudgetLedger
from .config import Settings
from .notify import DiscordDM, NotifyError, NotifySettings
from .status_client import StatusClient, StatusFetchError
from .watch import StateStore, StdoutNotifier, flush_pending, silence, watch_once

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _fail(code: str, detail: str, exit_code: int) -> int:
    print(json.dumps({"error": code, "detail": detail}, ensure_ascii=False), file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ops_agent", description="Read-only status analysis (Phase 0)")
    parser.add_argument("--force", action="store_true", help="analyze even if every service is operational")
    parser.add_argument("--dry-run", action="store_true", help="print the request instead of calling Bedrock")
    parser.add_argument("--watch", action="store_true", help="one watch cycle for a timer: decide, analyze once, notify")
    parser.add_argument("--silence", type=int, metavar="MINUTES", help="suppress watch notifications for this long (1-1440)")
    parser.add_argument("--check", action="store_true", help="verify the AWS setup (boto3, role, credentials) without calling the model")
    args = parser.parse_args(argv)
    modes = [name for name, on in (("--watch", args.watch), ("--silence", args.silence is not None), ("--check", args.check)) if on]
    if len(modes) > 1 or (modes and (args.dry_run or args.force)):
        # --watch really sends messages and spends money, so a "dry run" of it must not exist.
        return _fail("usage", "--watch, --silence and --check are exclusive and cannot be combined with --dry-run or --force", 64)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codepage

    settings = Settings.from_env()
    if args.silence is not None:
        try:
            until = silence(StateStore(settings.state_path), args.silence, time.time())
        except ValueError as exc:
            return _fail("usage", str(exc), 64)
        print(json.dumps({"silenced_until_epoch": int(until)}))
        return 0
    if args.check:
        return _check(settings)
    if args.watch:
        return _watch(settings)
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
            # No SDK retries: a retry after a read timeout can be billed again, and the budget
            # reservation covers exactly one attempt.
            config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 0, "mode": "standard"}),
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


def _check(settings: Settings) -> int:
    """Deployment self-test. A healthy watch cycle never imports boto3, so without this a missing
    package or a broken role would only show up during the first real incident."""
    try:
        import boto3  # the interpreter that runs the unit must be able to import it
        from botocore.config import Config

        session = boto3.Session(region_name=settings.bedrock_region)
        credentials = session.get_credentials()
        if credentials is None:
            return _fail("check_failed", "no AWS credentials were found for this profile", 3)
        credentials.get_frozen_credentials()  # forces the role to be assumed now
        session.client(
            "bedrock-runtime",
            config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 0, "mode": "standard"}),
        )
    except ImportError as exc:
        return _fail("check_failed", f"cannot import {exc.name or 'a required package'} with this interpreter", 3)
    except Exception as exc:  # noqa: BLE001 - report the type only: never credentials or request bodies
        return _fail("check_failed", f"{type(exc).__name__} while preparing the AWS client", 3)
    print(json.dumps({"check": "ok", "region": settings.bedrock_region, "model": settings.bedrock_model_id}))
    return 0


def _watch(settings: Settings) -> int:
    try:
        notify_settings = NotifySettings.from_env()
        notifier = DiscordDM(notify_settings) if notify_settings.configured else StdoutNotifier()
    except ValueError as exc:
        return _fail("notify_misconfigured", str(exc), 78)
    store = StateStore(settings.state_path)
    # A notice that could not be delivered goes out before anything is fetched: an unreachable
    # status API must not strand a message that was already paid for.
    flushed = flush_pending(notifier, store, time.time())
    if flushed is not None:
        print(json.dumps({"action": flushed.action, "detail": flushed.detail}, ensure_ascii=False), file=sys.stderr)
        if flushed.action == "notify_failed":
            return 4

    client = StatusClient(settings.status_api_base, settings.request_timeout_seconds)
    try:
        # Only the current status decides whether anything happens. The history is for the model.
        snapshot = {"status": client.status(), "history": {}}
    except (StatusFetchError, ValueError) as exc:
        return _fail("status_unavailable", str(exc), 3)

    def analyze_fn(snap):
        try:
            snap["history"] = client.history()
        except (StatusFetchError, ValueError):
            snap["history"] = {}  # an analysis without the 7 day history beats no notification
        import boto3  # only when something needs analyzing: a healthy cycle needs no AWS at all
        from botocore.config import Config

        bedrock = boto3.client(
            "bedrock-runtime",
            region_name=settings.bedrock_region,
            config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 0, "mode": "standard"}),
        )
        return analyze(settings, bedrock, snap, BudgetLedger(settings), force=True)

    result = watch_once(
        snapshot=snapshot,
        analyze_fn=analyze_fn,
        notifier=notifier,
        store=store,
        now=time.time(),
        status_url=settings.status_page_url,
        min_duration_seconds=settings.watch_min_duration_seconds,
        repeat_after_seconds=settings.watch_repeat_after_seconds,
    )
    print(json.dumps({"action": result.action, "detail": result.detail}, ensure_ascii=False), file=sys.stderr)
    return 4 if result.action == "notify_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
