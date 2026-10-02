from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings

logger = logging.getLogger("ops_agent")
JST = timezone(timedelta(hours=9), "JST")


class BudgetExceeded(RuntimeError):
    pass


def cost_jpy(settings: Settings, input_tokens: int, output_tokens: int) -> float:
    usd = (
        input_tokens * settings.price_input_usd_per_mtok
        + output_tokens * settings.price_output_usd_per_mtok
    ) / 1_000_000
    return usd * settings.usd_jpy


def _month_key(moment: datetime) -> str:
    return moment.astimezone(JST).strftime("%Y-%m")


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

    def record(self, now: datetime, *, input_tokens: int, output_tokens: int) -> float:
        cost = cost_jpy(self.settings, input_tokens, output_tokens)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "at": now.astimezone(timezone.utc).isoformat(),
                        "month": _month_key(now),
                        "model": self.settings.bedrock_model_id,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost_jpy": round(cost, 4),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return cost
