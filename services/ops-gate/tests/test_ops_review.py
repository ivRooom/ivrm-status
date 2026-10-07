"""Regression tests for review findings: compose file lookup, wall-clock timeouts, live player counts."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from test_ops_scripts import OPS, Shim, compose_dir, needs_sh, precheck, shim  # noqa: F401


# --- docker compose must be given -f ----------------------------------------------------------


@needs_sh
@pytest.mark.parametrize("name", ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"])
def test_every_usual_compose_file_name_is_found_and_passed_explicitly(shim: Shim, tmp_path: Path, name: str) -> None:
    directory = compose_dir(tmp_path, "main", (name,))
    shim.set("inspect_mc-main", "running healthy\n")
    result = shim.run("restart-mc-main.sh", IVRM_MC_MAIN_DIR=str(directory))
    assert result.returncode == 0, result.stderr
    assert f"docker compose --project-directory {directory} -f {directory}/{name} restart" in shim.calls()


@needs_sh
def test_a_directory_without_a_compose_file_fails_before_touching_docker(shim: Shim, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    for script, variable in (("restart-mc-main.sh", "IVRM_MC_MAIN_DIR"), ("start-mc-resource.sh", "IVRM_MC_RESOURCE_DIR")):
        result = shim.run(script, **{variable: str(empty)})
        assert result.returncode != 0 and "no compose file" in result.stderr
    assert shim.calls() == []


@needs_sh
def test_the_fake_docker_really_cannot_find_a_compose_file_without_dash_f(shim: Shim, tmp_path: Path) -> None:
    # Guards the guard: if the shim stopped modelling the lookup, the -f tests would prove nothing.
    environment = {"PATH": f"{shim.dir}:/usr/bin:/bin", "SHIM_DIR": str(shim.dir), "SHIM_LOG": str(shim.log)}
    directory = compose_dir(tmp_path, "d", ("compose.yml",))
    result = subprocess.run(
        ["docker", "compose", "--project-directory", str(directory), "restart"],
        capture_output=True, text=True, env=environment, cwd=str(tmp_path),
    )
    assert result.returncode == 1 and "no configuration file" in result.stderr


# --- the timeout is wall time -------------------------------------------------------------------


@needs_sh
def test_the_timeout_counts_wall_time_so_a_slow_probe_cannot_stretch_it(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "herta", ("docker-compose.prod.yml", ".env.production"))
    shim.set("compose_ps", "bot\nworker\n")  # the services come up; healthz never answers (no curl_ok)
    result = shim.run("restart-herta.sh", IVRM_HERTA_DIR=str(directory), IVRM_WAIT_SECONDS="30", CURL_SECONDS="5")
    assert result.returncode != 0 and "healthz" in result.stderr
    curls = [call for call in shim.calls() if call.startswith("curl")]
    # Each round costs 5s (curl) + 3s (sleep) of wall time, so 30s allows about four rounds.
    # Counting only the sleeps would allow ten.
    assert 3 <= len(curls) <= 5, len(curls)
    elapsed = int((shim.dir / "clock").read_text()) - 1000
    assert elapsed < 30 + 8 + 20  # the timeout plus one round (the docker calls add a little)


# --- a player count is trusted only when it is live ----------------------------------------------


class Status(BaseHTTPRequestHandler):
    body = b"{}"

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture()
def serve():
    server = HTTPServer(("127.0.0.1", 0), Status)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def publish(body) -> str:
        Status.body = json.dumps(body).encode()
        return f"http://127.0.0.1:{server.server_port}/"

    yield publish
    server.shutdown()


def iso(age_seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - age_seconds, timezone.utc).isoformat()


def status(players, *, probe="reachable", state="operational", age=10, checked="default", kind="minecraft"):
    service = {"id": "x", "status": state, "meta": {"type": kind, "playersOnline": players, "probeStatus": probe}}
    if checked == "default":
        service["checked_at"] = iso(age)
    elif checked is not None:
        service["checked_at"] = checked
    return {"services": [service]}


def verdict(serve, body) -> int:
    return precheck(serve(body)).returncode


def test_a_fresh_live_zero_is_allowed(serve) -> None:
    assert verdict(serve, status(0, age=30)) == 0


@pytest.mark.parametrize("probe", ["indeterminate", None, "weird", ""])
def test_a_zero_without_a_live_probe_answer_is_refused(serve, probe) -> None:
    # The API reports playersOnline=0 even when it has no live data at all.
    result = precheck(serve(status(0, probe=probe)))
    assert result.returncode == 1 and "cannot tell" in result.stderr


def test_a_stale_observation_is_refused_even_if_it_says_zero(serve) -> None:
    assert verdict(serve, status(0, age=3600)) == 1
    assert verdict(serve, status(0, age=181)) == 1
    assert verdict(serve, status(0, age=170)) == 0


@pytest.mark.parametrize("checked", [None, "", "yesterday", 12345, "2026-10-07T01:00:00"])
def test_a_missing_or_unreadable_observation_time_is_refused(serve, checked) -> None:
    assert verdict(serve, status(0, checked=checked)) == 1


def test_an_observation_from_the_future_is_refused(serve) -> None:
    assert verdict(serve, status(0, age=-3600)) == 1


def test_an_unreachable_server_is_refused_even_when_it_is_reported_as_an_outage(serve) -> None:
    # A handshake that times out or is malformed gives exactly this state, and established
    # player sessions can survive it, so it proves nothing about who is connected.
    assert verdict(serve, status(0, probe="unreachable", state="outage")) == 1
    assert verdict(serve, status(5, probe="unreachable", state="outage")) == 1


@pytest.mark.parametrize("state", ["operational", "degraded", "unknown", "maintenance"])
def test_an_unreachable_probe_without_an_outage_is_refused(serve, state: str) -> None:
    assert verdict(serve, status(0, probe="unreachable", state=state)) == 1


def test_a_stale_outage_is_not_trusted_either(serve) -> None:
    assert verdict(serve, status(0, probe="unreachable", state="outage", age=3600)) == 1


@pytest.mark.parametrize("players", [1, 3])
def test_players_online_are_refused_whatever_else_is_true(serve, players: int) -> None:
    assert verdict(serve, status(players)) == 1


@pytest.mark.parametrize("body", [{}, {"services": []}, [], status(None), status("0"), status(-1), status(True), status(0, kind="other")])
def test_unusable_bodies_are_still_refused(serve, body) -> None:
    assert verdict(serve, body) == 1
