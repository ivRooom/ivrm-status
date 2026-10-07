from __future__ import annotations

import json
import urllib.request
from typing import Any
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 1_000_000
ALLOWED_PATHS = {"/api/status.json", "/api/status-history.json"}


class StatusFetchError(RuntimeError):
    pass


class StatusClient:
    """Read-only client for the public Status API. Only two fixed paths are reachable."""

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        parts = urlsplit(base_url)
        local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise ValueError("status api base must be https (http is allowed for localhost only)")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get(self, path: str, query: str = "") -> Any:
        if path not in ALLOWED_PATHS:
            raise StatusFetchError(f"path not allowed: {path}")
        url = f"{self.base_url}{path}{'?' + query if query else ''}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except OSError as exc:
            raise StatusFetchError(f"request failed: {exc}") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise StatusFetchError("response too large")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise StatusFetchError("response is not json") from exc

    def status(self) -> Any:
        return self.get("/api/status.json")

    def history(self) -> Any:
        return self.get("/api/status-history.json", "days=7")

    def snapshot(self) -> dict[str, Any]:
        return {"status": self.status(), "history": self.history()}
