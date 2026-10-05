from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

from ops_gate.__main__ import main
from ops_gate.gate import GateConfig, Operation, execute, load_config, sign_ticket

APPROVER = "123456789012345678"
OTHER = "987654321098765432"
SECRET = b"s" * 48
NOW = 1_800_000_000.0


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.marker = tmp_path / "marker"
        python = sys.executable
        write_marker = f"import pathlib; pathlib.Path(r'{self.marker}').write_text('ran')"
        self.operations = {
            "start_mc_resource": Operation("start_mc_resource", [python, "-c", write_marker], timeout_seconds=20),
            "restart_herta": Operation("restart_herta", [python, "-c", "raise SystemExit(3)"], timeout_seconds=20),
            "slow_op": Operation("slow_op", [python, "-c", "import time; time.sleep(10)"], timeout_seconds=1),
            "cooldown_op": Operation("cooldown_op", [python, "-c", write_marker], timeout_seconds=20, cooldown_seconds=3600),
            "guarded_op": Operation(
                "guarded_op",
                [python, "-c", write_marker],
                timeout_seconds=20,
                precheck_argv=[python, "-c", "raise SystemExit(1)"],
            ),
        }
        self.config = GateConfig(
            approver_discord_ids=frozenset({APPROVER}),
            secret=SECRET,
            state_dir=tmp_path / "state",
            audit_log=tmp_path / "audit.jsonl",
            max_ticket_age_seconds=900,
            operations=self.operations,
        )

    def ticket(self, **overrides):
        payload = {
            "v": 1,
            "op": "start_mc_resource",
            "proposal_id": "prop-00000001",
            "proposal_hash": "a" * 64,
            "approver_discord_id": APPROVER,
            "issued_at": int(NOW) - 10,
            "expires_at": int(NOW) + 600,
            "nonce": f"nonce-{time.monotonic_ns()}-0123456789",
        }
        payload.update(overrides)
        return sign_ticket(SECRET, payload)

    def run(self, ticket, **kwargs):
        return execute(self.config, *ticket, now=kwargs.pop("now", NOW), **kwargs)

    def audit(self) -> list[dict]:
        if not self.config.audit_log.exists():
            return []
        return [json.loads(line) for line in self.config.audit_log.read_text(encoding="utf-8").splitlines()]


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_valid_ticket_runs_the_operation_and_is_audited(env: Env) -> None:
    result = env.run(env.ticket())
    assert result.status == "executed" and result.exit_code == 0
    assert env.marker.read_text() == "ran"
    record = env.audit()[-1]
    assert record["result"] == "executed" and record["op"] == "start_mc_resource"
    assert record["approver_discord_id"] == APPROVER and record["proposal_id"] == "prop-00000001"
    assert SECRET.decode() not in json.dumps(record)


def test_a_ticket_works_only_once(env: Env) -> None:
    ticket = env.ticket()
    assert env.run(ticket).status == "executed"
    env.marker.unlink()
    again = env.run(ticket)
    assert again.status == "denied" and again.reason == "ticket_already_used"
    assert not env.marker.exists()


def test_tampered_payload_is_refused(env: Env) -> None:
    payload_b64, signature = env.ticket()
    body = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    body["op"] = "restart_herta"
    forged = base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()
    result = env.run((forged, signature))
    assert result.status == "denied" and result.reason == "signature_invalid"
    assert not env.marker.exists()


def test_ticket_signed_with_another_secret_is_refused(env: Env) -> None:
    payload = json.loads(base64.urlsafe_b64decode((p := env.ticket())[0] + "=="))
    forged = sign_ticket(b"x" * 48, payload)
    assert env.run(forged).reason == "signature_invalid"
    assert not env.marker.exists()


@pytest.mark.parametrize("signature", ["", "zz", "A" * 64, "0" * 63])
def test_malformed_signature_is_refused(env: Env, signature: str) -> None:
    assert env.run((env.ticket()[0], signature)).status == "denied"
    assert not env.marker.exists()


def test_only_the_configured_approver_is_accepted(env: Env) -> None:
    result = env.run(env.ticket(approver_discord_id=OTHER))
    assert result.reason == "approver_not_allowed"
    assert not env.marker.exists()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"expires_at": int(NOW) - 1, "issued_at": int(NOW) - 100}, "ticket_expired"),
        ({"issued_at": int(NOW) + 3600, "expires_at": int(NOW) + 3700}, "ticket_not_yet_valid"),
        ({"issued_at": int(NOW) - 10, "expires_at": int(NOW) + 100000}, "ticket_lifetime_invalid"),
        ({"issued_at": int(NOW), "expires_at": int(NOW) - 5}, "ticket_lifetime_invalid"),
    ],
)
def test_ticket_time_window_is_enforced(env: Env, overrides: dict, reason: str) -> None:
    assert env.run(env.ticket(**overrides)).reason == reason
    assert not env.marker.exists()


@pytest.mark.parametrize("op", ["unknown_op", "rm -rf /", "restart_herta; id", "../etc", "START"])
def test_unknown_or_malicious_operation_names_are_refused(env: Env, op: str) -> None:
    result = env.run(env.ticket(op=op))
    assert result.status == "denied"
    assert result.reason in {"operation_not_allowed", "ticket_fields_invalid"}
    assert not env.marker.exists()


def test_arguments_are_never_interpreted_by_a_shell(env: Env) -> None:
    hostile = "a; touch HACKED & echo $(id) `id` | cat > HACKED"
    env.config.operations["echo_arg"] = Operation(
        "echo_arg", [sys.executable, "-c", "import sys; print(sys.argv[1])", hostile], timeout_seconds=20
    )
    result = env.run(env.ticket(op="echo_arg"))
    assert result.status == "executed"
    assert result.output.strip() == hostile  # passed through as one literal argument
    assert not (Path.cwd() / "HACKED").exists()


def test_a_signed_ticket_cannot_smuggle_a_command(env: Env) -> None:
    result = env.run(env.ticket(argv=["id"]))
    assert result.reason == "ticket_fields_invalid"
    payload = {"v": 1, "op": "start_mc_resource"}
    assert env.run(sign_ticket(SECRET, payload)).reason == "ticket_fields_invalid"


@pytest.mark.parametrize(
    "overrides",
    [{"issued_at": "1"}, {"issued_at": True}, {"nonce": "short"}, {"proposal_hash": "xyz"}, {"approver_discord_id": 5}, {"v": 2}],
)
def test_field_types_are_validated(env: Env, overrides: dict) -> None:
    assert env.run(env.ticket(**overrides)).status == "denied"
    assert not env.marker.exists()


def test_oversized_ticket_is_refused(env: Env) -> None:
    assert env.run(("A" * 5000, "0" * 64)).status == "denied"


def test_failed_command_is_reported_and_the_ticket_is_consumed(env: Env) -> None:
    ticket = env.ticket(op="restart_herta")
    result = env.run(ticket)
    assert result.status == "failed" and result.exit_code == 3
    assert env.run(ticket).reason == "ticket_already_used"
    assert env.audit()[0]["result"] == "failed"


def test_timeout_is_reported(env: Env) -> None:
    result = env.run(env.ticket(op="slow_op"))
    assert result.status == "failed" and result.reason == "timeout"
    assert env.audit()[-1]["result"] == "timeout"


def test_cooldown_blocks_a_second_run_until_it_passes(env: Env) -> None:
    assert env.run(env.ticket(op="cooldown_op")).status == "executed"
    env.marker.unlink()
    blocked = env.run(env.ticket(op="cooldown_op"), now=NOW + 60)
    assert blocked.status == "denied" and blocked.reason == "cooldown_active" and blocked.busy
    assert not env.marker.exists()
    later = NOW + 4000
    ticket = env.ticket(op="cooldown_op", issued_at=int(later) - 10, expires_at=int(later) + 600)
    assert env.run(ticket, now=later).status == "executed"


def test_failed_precheck_refuses_the_operation(env: Env) -> None:
    ticket = env.ticket(op="guarded_op")
    result = env.run(ticket)
    assert result.status == "denied" and result.reason == "precheck_failed"
    assert not env.marker.exists()
    assert env.run(ticket).reason == "ticket_already_used"


def test_dry_run_checks_without_running_or_consuming(env: Env) -> None:
    ticket = env.ticket()
    assert env.run(ticket, dry_run=True).reason == "dry_run_ok"
    assert not env.marker.exists()
    assert env.run(ticket).status == "executed"


def test_dry_run_still_rejects_invalid_tickets(env: Env) -> None:
    assert env.run(env.ticket(approver_discord_id=OTHER), dry_run=True).reason == "approver_not_allowed"


def test_denials_are_audited_without_running_anything(env: Env) -> None:
    env.run(env.ticket(approver_discord_id=OTHER))
    record = env.audit()[-1]
    assert record["result"] == "denied" and record["reason"] == "approver_not_allowed"
    assert record["approver_discord_id"] == OTHER  # who tried is kept for investigation


# --- config -------------------------------------------------------------------


def _write_config(tmp_path: Path, **overrides) -> Path:
    secret = tmp_path / "secret"
    secret.write_bytes(overrides.pop("secret", SECRET))
    if os.name == "posix":
        secret.chmod(overrides.pop("mode", 0o600))
    config = {
        "approver_discord_ids": [APPROVER],
        "ticket_secret_file": str(secret),
        "state_dir": str(tmp_path / "state"),
        "audit_log": str(tmp_path / "audit.jsonl"),
        "operations": {"start_mc_resource": {"argv": [sys.executable, "-c", "pass"]}, "disabled_op": {"argv": []}},
    }
    config.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_config_loads_and_empty_argv_means_disabled(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    assert set(config.operations) == {"start_mc_resource"}


def test_short_secret_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, secret=b"short"))


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX only")
def test_group_readable_secret_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, mode=0o640))


@pytest.mark.parametrize("ids", [[], ["abc"], ["12345"], [""]])
def test_invalid_approver_ids_are_rejected(tmp_path: Path, ids: list) -> None:
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, approver_discord_ids=ids))


def test_invalid_operation_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_config(_write_config(tmp_path, operations={"Bad Name": {"argv": ["true"]}}))


# --- CLI (SSH forced command) ---------------------------------------------------


def test_cli_ignores_argv_and_uses_ssh_original_command(tmp_path: Path, monkeypatch, capsys) -> None:
    config_path = _write_config(tmp_path)
    env = Env(tmp_path)
    ticket, signature = env.ticket()
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"check {ticket} {signature}")
    monkeypatch.setattr("ops_gate.gate.time.time", lambda: NOW)
    code = main(["--config", str(config_path), "run", "evil", "args"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["reason"] == "dry_run_ok"


@pytest.mark.parametrize("original", ["bash -c id", "run", "run a b c", "cat /etc/passwd", "check 'unterminated"])
def test_cli_rejects_anything_that_is_not_a_ticket_request(tmp_path: Path, monkeypatch, capsys, original: str) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", original)
    code = main(["--config", str(_write_config(tmp_path))])
    assert code == 64
    assert json.loads(capsys.readouterr().out)["reason"] == "usage"


def test_cli_reports_a_misconfigured_gate_without_leaking_details(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"run {'a' * 20} {'0' * 64}")
    code = main(["--config", str(tmp_path / "missing.json")])
    assert code == 11
    assert json.loads(capsys.readouterr().out)["reason"] == "gate_misconfigured"
