"""Approval gate: runs an allowlisted server operation only for a valid signed ticket.

The gate is the last line of defense on the server. It never takes a command from
the caller: the caller can only name an operation that the host's own config maps
to a fixed argv, and only with a ticket signed by the approval service.

Threat model: a leaked SSH key, a compromised dashboard session or a misbehaving
LLM must not be enough to run anything. The ticket binds the operation, the
proposal, the approver and an expiry; each ticket works once.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TICKET_VERSION = 1
MAX_TICKET_BYTES = 2048
MAX_CLOCK_SKEW_SECONDS = 30
MAX_OUTPUT_CHARS = 2000

_OP_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_DISCORD_ID = re.compile(r"^[0-9]{17,20}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX = re.compile(r"^[0-9a-f]{64}$")


class Denied(Exception):
    """The request was refused. The reason is logged; the caller only sees a code."""

    def __init__(self, reason: str, *, busy: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.busy = busy


@dataclass(frozen=True)
class Operation:
    name: str
    argv: list[str]
    timeout_seconds: int = 120
    cooldown_seconds: int = 0
    # Optional guard run before the operation; a non-zero exit refuses it
    # (for example "no players online"). Same rules: fixed argv, no shell.
    precheck_argv: list[str] | None = None


@dataclass(frozen=True)
class GateConfig:
    approver_discord_ids: frozenset[str]
    secret: bytes
    state_dir: Path
    audit_log: Path
    max_ticket_age_seconds: int = 900
    operations: dict[str, Operation] = field(default_factory=dict)


_SECRET_TEXT = re.compile(rb"^[!-~]{32,}$")


def _read_secret(path: Path) -> bytes:
    """The secret is printable ASCII text (e.g. `openssl rand -hex 32`).

    Only a single trailing newline is removed. Raw random bytes are not accepted: stripping
    whitespace bytes would silently turn them into a different key than the signer uses.
    """
    data = path.read_bytes()
    for ending in (b"\r\n", b"\n"):
        if data.endswith(ending):
            data = data[: -len(ending)]
            break
    if not _SECRET_TEXT.fullmatch(data):
        raise ValueError(
            "ticket secret must be at least 32 printable ASCII characters without spaces "
            "(generate one with: openssl rand -hex 32)"
        )
    return data


def _bounded(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def load_config(path: Path) -> GateConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    secret_path = Path(raw["ticket_secret_file"])
    if os.name == "posix" and secret_path.stat().st_mode & 0o077:
        raise ValueError("ticket secret file must not be readable by group or others (chmod 600)")
    secret = _read_secret(secret_path)

    approvers = frozenset(str(item) for item in raw["approver_discord_ids"])
    if not approvers or not all(_DISCORD_ID.fullmatch(item) for item in approvers):
        raise ValueError("approver_discord_ids must be a non-empty list of Discord user ids")

    operations: dict[str, Operation] = {}
    for name, spec in raw.get("operations", {}).items():
        if not _OP_NAME.fullmatch(name):
            raise ValueError(f"invalid operation name: {name}")
        argv = spec.get("argv")
        if not argv:
            continue  # an entry with an empty argv is disabled
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError(f"{name}: argv must be a list of strings")
        precheck = spec.get("precheck_argv")
        operations[name] = Operation(
            name=name,
            argv=argv,
            timeout_seconds=_bounded(f"{name}.timeout_seconds", spec.get("timeout_seconds", 120), 1, 3600),
            cooldown_seconds=_bounded(f"{name}.cooldown_seconds", spec.get("cooldown_seconds", 0), 0, 86_400),
            precheck_argv=precheck if precheck else None,
        )
    return GateConfig(
        approver_discord_ids=approvers,
        secret=secret,
        state_dir=Path(raw["state_dir"]),
        audit_log=Path(raw["audit_log"]),
        max_ticket_age_seconds=_bounded("max_ticket_age_seconds", raw.get("max_ticket_age_seconds", 900), 1, 3600),
        operations=operations,
    )


# --- tickets -----------------------------------------------------------------


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign_ticket(secret: bytes, payload: dict[str, Any]) -> tuple[str, str]:
    """Return (payload_b64url, signature_hex). The approval service does this."""
    body = _canonical(payload)
    return (
        base64.urlsafe_b64encode(body).rstrip(b"=").decode(),
        hmac.new(secret, body, hashlib.sha256).hexdigest(),
    )


def _decode_ticket(payload_b64: str, signature_hex: str, secret: bytes) -> dict[str, Any]:
    if len(payload_b64) > MAX_TICKET_BYTES * 2 or not _B64URL.fullmatch(payload_b64):
        raise Denied("ticket_malformed")
    if not _HEX.fullmatch(signature_hex):
        raise Denied("signature_malformed")
    try:
        body = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    except (binascii.Error, ValueError):
        raise Denied("ticket_malformed") from None
    if len(body) > MAX_TICKET_BYTES:
        raise Denied("ticket_too_large")

    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_hex):
        raise Denied("signature_invalid")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise Denied("ticket_malformed") from None
    if not isinstance(payload, dict):
        raise Denied("ticket_malformed")
    return payload


def _validate_payload(payload: dict[str, Any], config: GateConfig, now: float) -> Operation:
    expected_keys = {"v", "op", "proposal_id", "proposal_hash", "approver_discord_id", "issued_at", "expires_at", "nonce"}
    if set(payload) != expected_keys:
        raise Denied("ticket_fields_invalid")
    if payload["v"] != TICKET_VERSION:
        raise Denied("ticket_version_unsupported")

    checks = (
        (payload["op"], _OP_NAME),
        (payload["proposal_id"], _PROPOSAL_ID),
        (payload["proposal_hash"], _HASH),
        (payload["approver_discord_id"], _DISCORD_ID),
        (payload["nonce"], _NONCE),
    )
    if not all(isinstance(value, str) and pattern.fullmatch(value) for value, pattern in checks):
        raise Denied("ticket_fields_invalid")
    issued, expires = payload["issued_at"], payload["expires_at"]
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (issued, expires)):
        raise Denied("ticket_fields_invalid")

    if payload["approver_discord_id"] not in config.approver_discord_ids:
        raise Denied("approver_not_allowed")
    if expires <= issued or expires - issued > config.max_ticket_age_seconds:
        raise Denied("ticket_lifetime_invalid")
    if now < issued - MAX_CLOCK_SKEW_SECONDS:
        raise Denied("ticket_not_yet_valid")
    if now >= expires:
        raise Denied("ticket_expired")

    operation = config.operations.get(payload["op"])
    if operation is None:
        raise Denied("operation_not_allowed")
    return operation


# --- state: single use, cooldown, single flight -------------------------------


def _claim_nonce(config: GateConfig, nonce: str, expires_at: int, now: float) -> None:
    directory = config.state_dir / "nonces"
    directory.mkdir(parents=True, exist_ok=True)
    # A record is stamped with its ticket's expiry; once that has passed the ticket is
    # refused as expired anyway, so the record can go. Same clock as the checks.
    for entry in directory.iterdir():
        if entry.stat().st_mtime + MAX_CLOCK_SKEW_SECONDS < now:
            entry.unlink(missing_ok=True)
    path = directory / nonce
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Denied("ticket_already_used") from None
    os.close(descriptor)
    os.utime(path, (expires_at, expires_at))


def _check_cooldown(config: GateConfig, operation: Operation, now: float) -> None:
    if operation.cooldown_seconds <= 0:
        return
    marker = config.state_dir / "last" / operation.name
    if marker.exists() and now - marker.stat().st_mtime < operation.cooldown_seconds:
        raise Denied("cooldown_active", busy=True)


def _touch_last(config: GateConfig, operation: Operation, moment: float) -> None:
    marker = config.state_dir / "last" / operation.name
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(moment), encoding="utf-8")
    os.utime(marker, (moment, moment))


def _lock_nonblocking(handle: Any) -> None:
    if os.name == "nt":  # development on Windows; production is POSIX
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _Lock:
    """One operation at a time, enforced by the kernel.

    flock / LockFileEx is released automatically if the gate process dies, so there is
    no stale-lock guessing. (An earlier version expired the lock file using the timeout
    of the *requesting* operation, which could let a short operation take the lock away
    from a long one that was still running.) The file is never unlinked: unlinking is
    what makes file locks racy.
    """

    def __init__(self, config: GateConfig) -> None:
        self.path = config.state_dir / "gate.lock"
        self.handle: Any = None

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - held until __exit__
        try:
            _lock_nonblocking(handle)
        except OSError:
            handle.close()
            raise Denied("another_operation_running", busy=True) from None
        self.handle = handle
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.handle is not None:
            try:
                _unlock(self.handle)
            finally:
                self.handle.close()
                self.handle = None


def _nonce_consumed(config: GateConfig, nonce: str) -> bool:
    return (config.state_dir / "nonces" / nonce).exists()


def _audit_writable(config: GateConfig) -> None:
    """Fail closed: do not run anything that could not be recorded afterwards."""
    config.audit_log.parent.mkdir(parents=True, exist_ok=True)
    with config.audit_log.open("a", encoding="utf-8"):
        pass


# --- running ------------------------------------------------------------------


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv from the host's own config, never a shell
        argv,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")},
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _tail(text: str) -> str:
    return text[-MAX_OUTPUT_CHARS:]


def _audit(config: GateConfig, record: dict[str, Any]) -> None:
    config.audit_log.parent.mkdir(parents=True, exist_ok=True)
    with config.audit_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


@dataclass(frozen=True)
class Result:
    status: str  # executed | failed | denied
    reason: str = ""
    exit_code: int | None = None
    output: str = ""
    busy: bool = False


def execute(
    config: GateConfig,
    payload_b64: str,
    signature_hex: str,
    *,
    dry_run: bool = False,
    now: float | None = None,
) -> Result:
    """Verify the ticket and, unless dry_run, run the operation. Never raises."""
    moment = time.time() if now is None else now
    base: dict[str, Any] = {"at": int(moment), "dry_run": dry_run}
    payload: dict[str, Any] = {}
    try:
        payload = _decode_ticket(payload_b64, signature_hex, config.secret)
        operation = _validate_payload(payload, config, moment)
        base.update(
            op=operation.name,
            proposal_id=payload["proposal_id"],
            proposal_hash=payload["proposal_hash"],
            approver_discord_id=payload["approver_discord_id"],
        )
        _audit_writable(config)
        _check_cooldown(config, operation, moment)
        if _nonce_consumed(config, payload["nonce"]):
            raise Denied("ticket_already_used")  # check must agree with what run would do
        if dry_run:
            _audit(config, {**base, "result": "dry_run_ok"})
            return Result(status="executed", reason="dry_run_ok")

        with _Lock(config):
            # Re-check under the lock: two tickets for the same operation can both pass the
            # early check while the first one runs; the second must still see its cooldown.
            _check_cooldown(config, operation, moment)
            # Consume the ticket before running: a failed run must not be replayable.
            _claim_nonce(config, payload["nonce"], payload["expires_at"], moment)
            if operation.precheck_argv:
                try:
                    pre = _run(operation.precheck_argv, min(operation.timeout_seconds, 30))
                except (subprocess.TimeoutExpired, OSError):
                    raise Denied("precheck_failed") from None
                if pre.returncode != 0:
                    raise Denied("precheck_failed")

            started = time.time()
            try:
                completed = _run(operation.argv, operation.timeout_seconds)
            except subprocess.TimeoutExpired:
                _touch_last(config, operation, moment)
                return _recorded(
                    config,
                    {**base, "result": "timeout", "duration": round(time.time() - started, 2)},
                    Result(status="failed", reason="timeout"),
                )
            except OSError as exc:
                return _recorded(
                    config,
                    {**base, "result": "spawn_error", "detail": type(exc).__name__},
                    Result(status="failed", reason="spawn_error"),
                )

            _touch_last(config, operation, moment)
            ok = completed.returncode == 0
            return _recorded(
                config,
                {
                    **base,
                    "result": "executed" if ok else "failed",
                    "exit_code": completed.returncode,
                    "duration": round(time.time() - started, 2),
                    "stderr_tail": _tail(completed.stderr),
                },
                Result(
                    status="executed" if ok else "failed",
                    exit_code=completed.returncode,
                    output=_tail(completed.stdout),
                ),
            )
    except Denied as denied:
        # The payload is only echoed after its signature verified, so these fields are ours.
        known = {k: payload[k] for k in ("op", "proposal_id", "approver_discord_id") if k in payload}
        result = Result(status="denied", reason=denied.reason, busy=denied.busy)
        return _recorded(config, {**known, **base, "result": "denied", "reason": denied.reason}, result)
    except Exception as exc:  # noqa: BLE001 - state or audit I/O failed: report, never a traceback
        try:
            _audit(config, {**base, "result": "failed", "reason": "internal_error", "detail": type(exc).__name__})
        except Exception:  # noqa: BLE001 - nothing more can be done; the caller still gets a code
            pass
        return Result(status="failed", reason="internal_error")


def _recorded(config: GateConfig, record: dict[str, Any], result: Result) -> Result:
    """Write the audit record; if that fails after the fact, say so instead of hiding the outcome."""
    try:
        _audit(config, record)
    except Exception:  # noqa: BLE001
        if result.status == "denied":
            return Result(status="failed", reason="internal_error")
        return Result(
            status=result.status,
            reason="audit_write_failed",
            exit_code=result.exit_code,
            output=result.output,
        )
    return result
