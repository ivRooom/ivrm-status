from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import Settings

logger = logging.getLogger("ops_agent")
JST = timezone(timedelta(hours=9), "JST")


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    id: str
    cost_jpy: float


def cost_jpy(settings: Settings, input_tokens: int, output_tokens: int) -> float:
    usd = (
        input_tokens * settings.price_input_usd_per_mtok
        + output_tokens * settings.price_output_usd_per_mtok
    ) / 1_000_000
    return usd * settings.usd_jpy


def _month_key(moment: datetime) -> str:
    return moment.astimezone(JST).strftime("%Y-%m")


@contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """Cross-process lock around check-then-append, so two runs cannot both pass the check."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BudgetLedger:
    """Append-only JSONL ledger of estimated spend; the month is a JST calendar month.

    The numbers are estimates from token usage, not the AWS bill. A budget alarm on
    the AWS side remains the source of truth.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path: Path = settings.ledger_path

    def month_spent_jpy(self, now: datetime) -> float:
        if not self.path.exists():
            return 0.0
        key = _month_key(now)
        total = 0.0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("month") == key:
                total += float(record.get("cost_jpy", 0.0))
        return total

    def check(self, now: datetime) -> float:
        spent = self.month_spent_jpy(now)
        budget = self.settings.monthly_budget_jpy
        if spent >= budget:
            raise BudgetExceeded(f"monthly budget reached: {spent:.1f} / {budget:.0f} JPY")
        if spent >= budget * self.settings.budget_warn_ratio:
            logger.warning("ops_agent_budget_warning spent_jpy=%.1f budget_jpy=%.0f", spent, budget)
        return spent

    def _append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def reserve(self, now: datetime, *, input_tokens: int, output_tokens: int) -> "Reservation":
        """Durably book a conservative cost BEFORE the paid call.

        If the process crashes or the response times out after Bedrock has processed the
        request, the reservation stays on the ledger, so the spend is never unrecorded.
        """
        cost = cost_jpy(self.settings, input_tokens, output_tokens)
        reservation = Reservation(id=uuid.uuid4().hex, cost_jpy=cost)
        with _exclusive(self.path):
            spent = self.month_spent_jpy(now)
            budget = self.settings.monthly_budget_jpy
            if spent + cost > budget:
                # Refuse before the call: this reservation would not fit in what is left.
                raise BudgetExceeded(
                    f"reservation does not fit the monthly budget: {spent:.1f} + {cost:.1f} > {budget:.0f} JPY"
                )
            self._append(
                {
                    "kind": "reservation",
                    "id": reservation.id,
                    "at": now.astimezone(timezone.utc).isoformat(),
                    "month": _month_key(now),
                    "model": self.settings.bedrock_model_id,
                    "cost_jpy": round(cost, 4),
                }
            )
        return reservation

    def settle(
        self,
        now: datetime,
        reservation: "Reservation",
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> float:
        """Reconcile a reservation with the actual usage; returns the actual cost.

        Passing None for the usage means the call is known not to have been billed
        (for example it was rejected before processing) and releases the reservation.
        """
        actual = 0.0 if input_tokens is None or output_tokens is None else cost_jpy(self.settings, input_tokens, output_tokens)
        self._append(
            {
                "kind": "settlement",
                "reservation_id": reservation.id,
                "at": now.astimezone(timezone.utc).isoformat(),
                "month": _month_key(now),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_jpy": round(actual - reservation.cost_jpy, 4),  # the delta, so the month total equals the actual
            }
        )
        return actual
