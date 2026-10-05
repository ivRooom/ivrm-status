"""Regression tests for review findings: locking, cooldown, check, secret format, I/O failures."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ops_gate import gate as gate_module
from ops_gate.gate import GateConfig, Operation, _Lock, load_config

from test_gate import NOW, OTHER, Env, _write_config  # noqa: F401  (fixtures and helpers)
from test_gate import env  # noqa: F401  (pytest fixture)


def with_changes(config: GateConfig, **changes) -> GateConfig:
    values = {
        "approver_discord_ids": config.approver_discord_ids,
        "secret": config.secret,
        "state_dir": config.state_dir,
        "audit_log": config.audit_log,
        "max_ticket_age_seconds": config.max_ticket_age_seconds,
        "operations": config.operations,
    }
    values.update(changes)
    return GateConfig(**values)


# --- locking ------------------------------------------------------------------------


def test_only_one_operation_runs_at_a_time(env: Env) -> None:
    with _Lock(env.config):  # another operation is running
        for op in ("start_mc_resource", "restart_herta"):
            result = env.run(env.ticket(op=op))
            assert result.reason == "another_operation_running" and result.busy
    assert not env.marker.exists()
    assert env.run(env.ticket()).status == "executed"  # released afterwards


def test_a_short_operation_cannot_take_the_lock_from_a_long_one(env: Env) -> None:
    # The lock is held by the kernel, not guessed from a timestamp, so neither elapsed time
    # nor a shorter timeout on the requesting operation can steal it.
    later = NOW + 100_000
    with _Lock(env.config):
        ticket = env.ticket(op="start_mc_resource", issued_at=int(later) - 10, expires_at=int(later) + 600)
        assert env.run(ticket, now=later).reason == "another_operation_running"
    assert not env.marker.exists()


def test_concurrent_gates_do_not_run_two_operations(env: Env) -> None:
    started, release = env.tmp / "started", env.tmp / "release"
    script = (
        "import pathlib, time\n"
        f"pathlib.Path(r'{started}').write_text('1')\n"
        f"while not pathlib.Path(r'{release}').exists():\n"
        "    time.sleep(0.05)\n"
    )
    env.config.operations["long_op"] = Operation("long_op", [sys.executable, "-c", script], timeout_seconds=30)

    outcome: dict = {}
    worker = threading.Thread(target=lambda: outcome.update(first=env.run(env.ticket(op="long_op"))))
    worker.start()
    try:
        deadline = time.time() + 20
        while not started.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert started.exists()
        second = env.run(env.ticket(op="start_mc_resource"))
        assert second.reason == "another_operation_running" and second.busy
        assert not env.marker.exists()
    finally:
        release.write_text("1")
        worker.join(timeout=20)
    assert outcome["first"].status == "executed"


def test_a_lock_left_by_a_crashed_gate_does_not_block(env: Env) -> None:
    state = env.config.state_dir
    state.mkdir(parents=True, exist_ok=True)
    code = (
        "import os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "from pathlib import Path\n"
        "from ops_gate.gate import GateConfig, _Lock\n"
        f"cfg = GateConfig(frozenset(), b'x' * 32, Path(r'{state}'), Path('a'), 900, {{}})\n"
        "_Lock(cfg).__enter__()\n"
        "os._exit(0)  # dies while holding the lock, never releasing it\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parents[1])
    assert (state / "gate.lock").exists()  # the file is left behind...
    assert env.run(env.ticket()).status == "executed"  # ...but the kernel dropped the lock


# --- cooldown, check ----------------------------------------------------------------


def test_the_cooldown_is_rechecked_under_the_lock(env: Env, monkeypatch) -> None:
    assert env.run(env.ticket(op="cooldown_op")).status == "executed"
    env.marker.unlink()
    real = gate_module._check_cooldown
    calls: list[int] = []

    def early_check_passed_before_the_first_run_finished(config, operation, now):
        calls.append(1)
        if len(calls) == 1:
            return  # this ticket passed the early check while another run was still in flight
        real(config, operation, now)

    monkeypatch.setattr(gate_module, "_check_cooldown", early_check_passed_before_the_first_run_finished)
    result = env.run(env.ticket(op="cooldown_op"), now=NOW + 60)
    assert result.status == "denied" and result.reason == "cooldown_active"
    assert not env.marker.exists()
    assert len(calls) == 2


def test_check_reports_an_already_used_ticket(env: Env) -> None:
    ticket = env.ticket()
    assert env.run(ticket).status == "executed"
    checked = env.run(ticket, dry_run=True)
    assert checked.status == "denied" and checked.reason == "ticket_already_used"


# --- configuration ------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": -31},
        {"timeout_seconds": 99999},
        {"timeout_seconds": True},
        {"timeout_seconds": "120"},
        {"cooldown_seconds": -1},
        {"cooldown_seconds": 10**7},
    ],
)
def test_nonsensical_numbers_are_rejected(tmp_path: Path, spec: dict) -> None:
    operations = {"start_mc_resource": {"argv": [sys.executable, "-c", "pass"], **spec}}
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, operations=operations))


@pytest.mark.parametrize("age", [0, -5, 100000, "900"])
def test_ticket_age_limit_must_be_sane(tmp_path: Path, age) -> None:
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, max_ticket_age_seconds=age))


def test_the_secret_is_text_read_verbatim_apart_from_one_newline(tmp_path: Path) -> None:
    text = b"0123456789abcdef" * 4
    assert load_config(_write_config(tmp_path, secret=text + b"\n")).secret == text
    assert load_config(_write_config(tmp_path, secret=text + b"\r\n")).secret == text
    assert load_config(_write_config(tmp_path, secret=text)).secret == text


@pytest.mark.parametrize(
    "secret",
    [
        b"\n" + b"a" * 40,  # raw bytes starting with whitespace were silently stripped before
        b"a" * 40 + b"\n\n",  # more than one trailing newline
        b"a" * 40 + b" ",  # trailing space
        b"a" * 16 + b" " + b"a" * 24,  # inner space
        bytes(range(256)),  # raw random bytes
        b"a" * 31,  # too short
    ],
)
def test_raw_or_ambiguous_secrets_are_rejected(tmp_path: Path, secret: bytes) -> None:
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, secret=secret))


# --- I/O failures -------------------------------------------------------------------


def test_if_the_audit_log_cannot_be_written_nothing_runs(env: Env) -> None:
    blocker = env.tmp / "not-a-directory"
    blocker.write_text("x")
    env.config = with_changes(env.config, audit_log=blocker / "audit.jsonl")  # below a regular file
    result = env.run(env.ticket())
    assert result.status == "failed" and result.reason == "internal_error"
    assert not env.marker.exists()


def test_state_io_failures_are_reported_not_raised(env: Env) -> None:
    blocker = env.tmp / "state-is-a-file"
    blocker.write_text("x")
    env.config = with_changes(env.config, state_dir=blocker)
    result = env.run(env.ticket())
    assert result.status == "failed" and result.reason == "internal_error"
    assert not env.marker.exists()


def test_a_failing_audit_write_after_the_run_does_not_hide_the_outcome(env: Env, monkeypatch) -> None:
    real = gate_module._audit

    def audit(config, record):
        if record.get("result") == "executed":
            raise OSError("disk full")
        real(config, record)

    monkeypatch.setattr(gate_module, "_audit", audit)
    result = env.run(env.ticket())
    assert result.status == "executed" and result.reason == "audit_write_failed"
    assert env.marker.read_text() == "ran"


def test_a_denial_that_cannot_be_audited_is_still_a_clean_failure(env: Env, monkeypatch) -> None:
    def audit(_config, _record):
        raise OSError("disk full")

    monkeypatch.setattr(gate_module, "_audit", audit)
    result = env.run(env.ticket(approver_discord_id=OTHER))
    assert result.status == "failed" and result.reason == "internal_error"


# --- format checks must match the whole value (Python's $ also matches before a trailing newline)


@pytest.mark.parametrize(
    "overrides",
    [
        {"op": "start_mc_resource\n"},
        {"nonce": "n" * 20 + "\n"},
        {"proposal_id": "prop-00000001\n"},
        {"proposal_hash": "a" * 64 + "\n"},
        {"approver_discord_id": "123456789012345678\n"},
    ],
)
def test_a_trailing_newline_does_not_pass_a_format_check(env: Env, overrides: dict) -> None:
    result = env.run(env.ticket(**overrides))
    assert result.status == "denied" and result.reason == "ticket_fields_invalid"
    assert not env.marker.exists()


def test_a_trailing_newline_on_the_signature_or_payload_is_refused(env: Env) -> None:
    payload, signature = env.ticket()
    assert env.run((payload, signature + "\n")).reason == "signature_malformed"
    assert env.run((payload + "\n", signature)).reason == "ticket_malformed"
