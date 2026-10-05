from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


@dataclass(frozen=True, slots=True)
class Settings:
    status_api_base: str = "https://status.ivrm.jp"
    bedrock_region: str = "ap-northeast-1"
    # Japan inference profile. Keep it configurable: model ids change over time.
    bedrock_model_id: str = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    max_output_tokens: int = 700
    request_timeout_seconds: float = 5.0
    monthly_budget_jpy: float = 1500.0
    # Hard stop at 100% of the budget; a warning is logged from this share.
    budget_warn_ratio: float = 0.8
    usd_jpy: float = 150.0
    # USD per million tokens. Verify against current Bedrock pricing before relying on it.
    price_input_usd_per_mtok: float = 1.0
    price_output_usd_per_mtok: float = 5.0
    ledger_path: Path = Path("ops-agent-ledger.jsonl")

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        return cls(
            status_api_base=os.getenv("OPS_AGENT_STATUS_API_BASE", defaults.status_api_base).rstrip("/"),
            bedrock_region=os.getenv("OPS_AGENT_BEDROCK_REGION", defaults.bedrock_region),
            bedrock_model_id=os.getenv("OPS_AGENT_BEDROCK_MODEL_ID", defaults.bedrock_model_id),
            max_output_tokens=_int("OPS_AGENT_MAX_OUTPUT_TOKENS", defaults.max_output_tokens),
            request_timeout_seconds=_float("OPS_AGENT_REQUEST_TIMEOUT_SECONDS", defaults.request_timeout_seconds),
            monthly_budget_jpy=_float("OPS_AGENT_MONTHLY_BUDGET_JPY", defaults.monthly_budget_jpy),
            budget_warn_ratio=_float("OPS_AGENT_BUDGET_WARN_RATIO", defaults.budget_warn_ratio),
            usd_jpy=_float("OPS_AGENT_USD_JPY", defaults.usd_jpy),
            price_input_usd_per_mtok=_float("OPS_AGENT_PRICE_INPUT_USD_PER_MTOK", defaults.price_input_usd_per_mtok),
            price_output_usd_per_mtok=_float("OPS_AGENT_PRICE_OUTPUT_USD_PER_MTOK", defaults.price_output_usd_per_mtok),
            ledger_path=Path(os.getenv("OPS_AGENT_LEDGER_PATH", str(defaults.ledger_path))),
        )
