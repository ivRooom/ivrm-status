"""Fifth round: a dry run of --watch must not exist, and the deployment must be checkable."""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path

import pytest

from ops_agent import __main__ as cli

from test_deploy_files import ROOT, unit  # noqa: F401


def env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPS_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPS_AGENT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("OPS_AGENT_DISCORD_USER_ID", raising=False)
    monkeypatch.delenv("OPS_AGENT_DISCORD_BOT_TOKEN", raising=False)


# --- no "dry run" of something that really sends and spends --------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--watch", "--dry-run"],
        ["--watch", "--force"],
        ["--watch", "--silence", "10"],
        ["--watch", "--check"],
        ["--silence", "10", "--dry-run"],
        ["--check", "--dry-run"],
        ["--check", "--force"],
        ["--silence", "10", "--check"],
    ],
)
def test_conflicting_flags_are_refused_before_anything_runs(monkeypatch, tmp_path: Path, capsys, argv) -> None:
    env(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise AssertionError("nothing may run when the flags conflict")

    monkeypatch.setattr(cli, "StatusClient", boom)
    monkeypatch.setattr(cli, "build_notifier", boom)
    monkeypatch.setattr(cli, "DiscordDM", boom)
    monkeypatch.setattr(cli, "DiscordWebhook", boom)
    monkeypatch.setattr(cli, "_watch", boom)
    monkeypatch.setattr(cli, "_check", boom)
    assert cli.main(argv) == 64
    assert "usage" in capsys.readouterr().err
    assert not (tmp_path / "state.json").exists() and not (tmp_path / "state.json.silence").exists()


def test_dry_run_alone_still_prints_the_request(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path)

    class Client:
        def __init__(self, *_a, **_k) -> None: ...

        def snapshot(self):
            return {"status": {"services": []}, "history": {}}

    monkeypatch.setattr(cli, "StatusClient", Client)
    assert cli.main(["--dry-run"]) == 0
    assert "modelId" in capsys.readouterr().out


# --- --check finds the deployment problems that a healthy cycle hides -----------------------------


def fake_session(monkeypatch, *, credentials="ok"):
    import boto3

    class Frozen:
        pass

    class Credentials:
        def get_frozen_credentials(self):
            if credentials == "role-denied":
                raise RuntimeError("AccessDenied: not authorized to perform sts:AssumeRole AKIASECRETKEY")
            return Frozen()

    class Session:
        def __init__(self, **_k) -> None: ...

        def get_credentials(self):
            return None if credentials == "none" else Credentials()

        def client(self, *_a, **_k):
            return object()

    monkeypatch.setattr(boto3, "Session", Session)


def test_check_succeeds_without_calling_the_model(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path)
    fake_session(monkeypatch)
    assert cli.main(["--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["check"] == "ok" and out["region"] == "ap-northeast-1"


def test_check_reports_a_missing_boto3_the_way_the_unit_would_see_it(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path)
    real_import = builtins.__import__

    def no_boto3(name, *args, **kwargs):
        if name == "boto3" or name.startswith("boto3.") or name.startswith("botocore"):
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_boto3)
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    assert cli.main(["--check"]) == 3
    err = capsys.readouterr().err
    assert "check_failed" in err and "cannot import" in err and "boto3" in err


def test_check_reports_missing_credentials(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path)
    fake_session(monkeypatch, credentials="none")
    assert cli.main(["--check"]) == 3
    assert "no AWS credentials" in capsys.readouterr().err


def test_check_reports_a_role_that_cannot_be_assumed_without_leaking_details(monkeypatch, tmp_path: Path, capsys) -> None:
    env(monkeypatch, tmp_path)
    fake_session(monkeypatch, credentials="role-denied")
    assert cli.main(["--check"]) == 3
    err = capsys.readouterr().err
    assert "check_failed" in err and "RuntimeError" in err and "AKIASECRETKEY" not in err


# --- the unit and the documented install agree -----------------------------------------------------


def test_the_unit_runs_from_a_dedicated_venv() -> None:
    exec_start = unit("ivrm-ops-agent.service")["ExecStart"][0]
    assert exec_start == "/opt/ivrm-ops-agent/venv/bin/python -s -m ops_agent --watch"


def test_the_documented_install_puts_dependencies_where_the_unit_can_import_them() -> None:
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "python3.11 -m venv /opt/ivrm-ops-agent/venv" in deploy
    assert "/opt/ivrm-ops-agent/venv/bin/pip install -r /opt/ivrm-ops-agent/requirements.txt" in deploy
    assert "pip3.11 install" not in deploy  # a user-site install is invisible to a `-s` interpreter
    assert "-m ops_agent --check" in deploy  # the first-run section verifies what a healthy cycle hides
    assert (ROOT / "requirements.txt").is_file()


def test_every_documented_command_uses_the_venv_interpreter() -> None:
    deploy = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    in_block, commands = False, []
    for line in deploy.splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
        elif in_block and "-m ops_agent" in line:
            commands.append(line)
    assert len(commands) >= 2  # --check and --silence at least
    for line in commands:
        assert "/opt/ivrm-ops-agent/venv/bin/python" in line, line
