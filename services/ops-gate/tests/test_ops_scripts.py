"""The operation scripts, run with fake docker/curl/sleep on PATH (nothing real is touched)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(SH is None or os.name == "nt", reason="needs a POSIX sh")

DOCKER_SHIM = r"""#!/bin/sh
echo "docker $*" >> "$SHIM_LOG"
case "$1" in
  inspect) cat "$SHIM_DIR/inspect_$2" 2>/dev/null || exit 1 ;;
  compose)
    case "$*" in
      *" ps "*) cat "$SHIM_DIR/compose_ps" 2>/dev/null ;;
      *) [ -f "$SHIM_DIR/compose_fail" ] && exit 3 ;;
    esac ;;
esac
exit 0
"""
CURL_SHIM = r"""#!/bin/sh
echo "curl $*" >> "$SHIM_LOG"
[ -f "$SHIM_DIR/curl_ok" ]
"""
SLEEP_SHIM = "#!/bin/sh\nexit 0\n"  # never really wait


class Shim:
    def __init__(self, tmp: Path) -> None:
        self.dir = tmp / "shim"
        self.dir.mkdir()
        self.log = tmp / "calls.log"
        self.log.write_text("")
        for name, body in (("docker", DOCKER_SHIM), ("curl", CURL_SHIM), ("sleep", SLEEP_SHIM)):
            path = self.dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.tmp = tmp

    def set(self, name: str, content: str = "") -> None:
        (self.dir / name).write_text(content)

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.dir}:/usr/bin:/bin",
            "SHIM_DIR": str(self.dir),
            "SHIM_LOG": str(self.log),
            "IVRM_WAIT_SECONDS": "10",
            **env,
        }
        return subprocess.run([SH, str(OPS / script)], capture_output=True, text=True, env=environment, timeout=60)


@pytest.fixture()
def shim(tmp_path: Path) -> Shim:
    return Shim(tmp_path)


def compose_dir(tmp: Path, name: str, files: tuple[str, ...]) -> Path:
    directory = tmp / name
    directory.mkdir()
    for file in files:
        (directory / file).write_text("services: {}\n")
    return directory


@needs_sh
def test_start_mc_resource_runs_up_detached_and_waits_until_running(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "res", ("compose.yml",))
    shim.set("inspect_mc-resource", "running none\n")
    result = shim.run("start-mc-resource.sh", IVRM_MC_RESOURCE_DIR=str(directory))
    assert result.returncode == 0, result.stderr
    calls = shim.calls()
    assert f"docker compose --project-directory {directory} -f {directory}/compose.yml up -d" in calls
    assert not any(" restart" in call or " down" in call or " stop" in call for call in calls)


@needs_sh
def test_start_mc_resource_fails_when_the_container_never_becomes_ready(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "res", ("compose.yml",))
    shim.set("inspect_mc-resource", "running starting\n")
    result = shim.run("start-mc-resource.sh", IVRM_MC_RESOURCE_DIR=str(directory))
    assert result.returncode != 0 and "did not become ready" in result.stderr


@needs_sh
def test_a_missing_compose_file_fails_before_touching_docker(shim: Shim, tmp_path: Path) -> None:
    result = shim.run("start-mc-resource.sh", IVRM_MC_RESOURCE_DIR=str(tmp_path / "nope"))
    assert result.returncode != 0 and "not found" in result.stderr
    assert shim.calls() == []


@needs_sh
def test_restart_mc_main_restarts_exactly_once_then_only_reads(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "main", ("compose.yml",))
    shim.set("inspect_mc-main", "running healthy\n")
    result = shim.run("restart-mc-main.sh", IVRM_MC_MAIN_DIR=str(directory))
    assert result.returncode == 0, result.stderr
    restarts = [call for call in shim.calls() if " restart" in call]
    assert restarts == [f"docker compose --project-directory {directory} restart"]


@needs_sh
def test_restart_mc_main_is_not_ready_while_unhealthy(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "main", ("compose.yml",))
    shim.set("inspect_mc-main", "running unhealthy\n")
    result = shim.run("restart-mc-main.sh", IVRM_MC_MAIN_DIR=str(directory))
    assert result.returncode != 0
    assert len([call for call in shim.calls() if " restart" in call]) == 1  # never retried


@needs_sh
def test_restart_herta_restarts_bot_and_worker_with_absolute_paths(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "herta", ("docker-compose.prod.yml", ".env.production"))
    shim.set("compose_ps", "bot\nworker\n")
    shim.set("curl_ok")
    result = shim.run("restart-herta.sh", IVRM_HERTA_DIR=str(directory))
    assert result.returncode == 0, result.stderr
    expected = (
        f"docker compose --project-directory {directory} --env-file {directory}/.env.production "
        f"-f {directory}/docker-compose.prod.yml restart bot worker"
    )
    assert expected in shim.calls()
    assert any(call.startswith("curl") and "127.0.0.1:3000/healthz" in call for call in shim.calls())


@needs_sh
@pytest.mark.parametrize("running", ["bot\n", "worker\n", ""])
def test_restart_herta_requires_both_services_running(shim: Shim, tmp_path: Path, running: str) -> None:
    directory = compose_dir(tmp_path, "herta", ("docker-compose.prod.yml", ".env.production"))
    shim.set("compose_ps", running)
    shim.set("curl_ok")
    result = shim.run("restart-herta.sh", IVRM_HERTA_DIR=str(directory))
    assert result.returncode != 0 and "did not become ready" in result.stderr


@needs_sh
def test_restart_herta_fails_when_healthz_never_answers(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "herta", ("docker-compose.prod.yml", ".env.production"))
    shim.set("compose_ps", "bot\nworker\n")  # no curl_ok: healthz fails
    result = shim.run("restart-herta.sh", IVRM_HERTA_DIR=str(directory))
    assert result.returncode != 0 and "healthz" in result.stderr


@needs_sh
def test_a_failing_compose_command_stops_the_script(shim: Shim, tmp_path: Path) -> None:
    directory = compose_dir(tmp_path, "herta", ("docker-compose.prod.yml", ".env.production"))
    shim.set("compose_fail")
    shim.set("compose_ps", "bot\nworker\n")
    shim.set("curl_ok")
    result = shim.run("restart-herta.sh", IVRM_HERTA_DIR=str(directory))
    assert result.returncode != 0
    assert not any(call.startswith("curl") for call in shim.calls())  # did not go on to "verify"


@needs_sh
def test_no_script_uses_a_shell_construct_that_could_run_input(tmp_path: Path) -> None:
    for script in OPS.glob("*.sh"):
        text = script.read_text()
        assert "eval " not in text and "$(curl" not in text and "| sh" not in text and "| bash" not in text, script.name


@needs_sh
def test_scripts_are_valid_posix_sh() -> None:
    for script in OPS.glob("*.sh"):
        assert subprocess.run([SH, "-n", str(script)], capture_output=True).returncode == 0, script.name


# --- precheck_no_players.py -----------------------------------------------------------


class Status(BaseHTTPRequestHandler):
    body = b"{}"

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *_args) -> None:  # noqa: D401
        pass


@pytest.fixture()
def status_server():
    server = HTTPServer(("127.0.0.1", 0), Status)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield lambda body: (setattr(Status, "body", json.dumps(body).encode()), f"http://127.0.0.1:{server.server_port}/")[1]
    server.shutdown()


def precheck(url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(OPS / "precheck_no_players.py"), url], capture_output=True, text=True, timeout=30)


def status(players, kind="minecraft"):
    return {"services": [{"id": "x", "meta": {"type": kind, "playersOnline": players}}]}


def test_precheck_allows_a_restart_with_nobody_online(status_server) -> None:
    assert precheck(status_server(status(0))).returncode == 0


@pytest.mark.parametrize("players", [1, 7])
def test_precheck_refuses_when_players_are_online(status_server, players: int) -> None:
    result = precheck(status_server(status(players)))
    assert result.returncode == 1 and "player" in result.stderr


@pytest.mark.parametrize("body", [{}, {"services": []}, status(None), status("0"), status(-1), status(True), status(0, kind="other"), []])
def test_precheck_fails_closed_when_the_count_is_unknown(status_server, body) -> None:
    assert precheck(status_server(body)).returncode == 1


def test_precheck_fails_closed_when_the_status_is_unreachable() -> None:
    assert precheck("http://127.0.0.1:1/").returncode == 1


@pytest.mark.parametrize("url", ["http://status.ivrm.jp/api/status.json", "file:///etc/passwd", "ftp://x/y"])
def test_precheck_refuses_non_https_urls(url: str) -> None:
    result = precheck(url)
    assert result.returncode == 1 and "https" in result.stderr
