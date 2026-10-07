#!/usr/bin/env python3
"""Gate precheck: succeed only when nobody is connected to the Minecraft network.

    precheck_no_players.py <status.json URL>

Exit 0: zero players online, restart is safe.
Exit 1: players online, or the count could not be determined (fail closed).

Fail closed on purpose: a restart disconnects everyone. If this refuses, restart by hand
over SSH once you have decided that is acceptable. Python 3.9 compatible, stdlib only.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from urllib.parse import urlsplit

MAX_BYTES = 1_000_000


def online_players(status: object) -> int:
    services = status.get("services") if isinstance(status, dict) else None
    if not isinstance(services, list):
        raise ValueError("no services list")
    for service in services:
        meta = service.get("meta") if isinstance(service, dict) else None
        if isinstance(meta, dict) and meta.get("type") == "minecraft":
            count = meta.get("playersOnline")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("playersOnline is not a count")
            return count
    raise ValueError("no minecraft service in the status")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: precheck_no_players.py <status.json URL>", file=sys.stderr)
        return 1
    url = argv[1]
    parts = urlsplit(url)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        print("refusing a non-https status URL", file=sys.stderr)
        return 1
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:  # noqa: S310
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("response too large")
        count = online_players(json.loads(body))
    except (OSError, ValueError) as exc:
        print(f"cannot tell whether players are online ({type(exc).__name__}); refusing", file=sys.stderr)
        return 1
    if count > 0:
        print(f"{count} player(s) online; refusing to restart", file=sys.stderr)
        return 1
    print("no players online")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
