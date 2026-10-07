from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SEVERITIES = ("none", "info", "warning", "critical")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class Analysis:
    severity: str
    summary: str
    suspected_causes: list[str]
    next_steps: list[str]
    announcement_recommended: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "summary": self.summary,
            "suspected_causes": self.suspected_causes,
            "next_steps": self.next_steps,
            "announcement_recommended": self.announcement_recommended,
        }


def _text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    cleaned = _CONTROL.sub("", value).strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > limit:
        raise ValueError(f"{field} is too long")
    return cleaned


def _items(value: Any, field: str, *, max_items: int = 5, limit: int = 200) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{field} must be a list of at most {max_items} items")
    return [_text(item, limit, field) for item in value]


def validate_analysis(raw: Any) -> Analysis:
    """Strict validation: anything off-schema is rejected, never repaired."""
    if not isinstance(raw, dict):
        raise ValueError("analysis must be an object")
    allowed = {"severity", "summary", "suspected_causes", "next_steps", "announcement_recommended"}
    if set(raw) - allowed:
        raise ValueError("analysis has unexpected fields")
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        raise ValueError("severity is invalid")
    recommended = raw.get("announcement_recommended")
    if not isinstance(recommended, bool):
        raise ValueError("announcement_recommended must be a boolean")
    return Analysis(
        severity=severity,
        summary=_text(raw.get("summary"), 400, "summary"),
        suspected_causes=_items(raw.get("suspected_causes"), "suspected_causes"),
        next_steps=_items(raw.get("next_steps"), "next_steps"),
        announcement_recommended=recommended,
    )


REPORT_TOOL = {
    "toolSpec": {
        "name": "report_analysis",
        "description": "Report the analysis of the current service status.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "summary": {"type": "string", "description": "日本語で2〜3文。400字以内。"},
                    "suspected_causes": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    "next_steps": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    "announcement_recommended": {"type": "boolean"},
                },
                "required": [
                    "severity",
                    "summary",
                    "suspected_causes",
                    "next_steps",
                    "announcement_recommended",
                ],
                "additionalProperties": False,
            }
        },
    }
}
