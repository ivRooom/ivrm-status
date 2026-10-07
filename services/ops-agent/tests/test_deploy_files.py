from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def unit(name: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in (ROOT / "deploy" / name).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            values.setdefault(key.strip(), []).append(value.strip())
    return values


def test_the_service_runs_one_watch_cycle_as_an_unprivileged_user() -> None:
    service = unit("ivrm-ops-agent.service")
    assert service["Type"] == ["oneshot"] and service["User"] == ["ivrm-ops-agent"]
    assert service["ExecStart"][0].endswith("-m ops_agent --watch")
    assert service["EnvironmentFile"] == ["/etc/ivrm-ops-agent/env"]  # secrets are not in the unit


def test_the_service_is_sandboxed_and_can_only_write_its_own_state() -> None:
    service = unit("ivrm-ops-agent.service")
    for key in ("NoNewPrivileges", "PrivateTmp", "ProtectSystem", "ProtectHome", "RestrictSUIDSGID", "MemoryDenyWriteExecute"):
        assert key in service, key
    assert service["NoNewPrivileges"] == ["yes"] and service["ProtectSystem"] == ["strict"]
    assert service["ReadWritePaths"] == ["/var/lib/ivrm-ops-agent"]
    assert service["CapabilityBoundingSet"] == [""]  # no capabilities at all
    assert service["RestrictAddressFamilies"] == ["AF_INET AF_INET6"]  # no unix sockets: no docker.sock


def test_the_state_and_ledger_live_in_the_writable_directory() -> None:
    environment = " ".join(unit("ivrm-ops-agent.service")["Environment"])
    assert "OPS_AGENT_STATE_PATH=/var/lib/ivrm-ops-agent/" in environment
    assert "OPS_AGENT_LEDGER_PATH=/var/lib/ivrm-ops-agent/" in environment


def test_the_timer_runs_every_five_minutes() -> None:
    timer = unit("ivrm-ops-agent.timer")
    assert timer["OnUnitActiveSec"] == ["5min"] and timer["WantedBy"] == ["timers.target"]


def test_the_env_example_holds_no_secrets() -> None:
    text = (ROOT / "deploy" / "env.example").read_text(encoding="utf-8")
    assert re.search(r"^OPS_AGENT_DISCORD_BOT_TOKEN=$", text, re.M)
    assert re.search(r"^OPS_AGENT_DISCORD_USER_ID=$", text, re.M)
    assert not re.search(r"\d{17,20}", text)


def test_the_aws_examples_use_placeholders_and_no_long_lived_keys() -> None:
    config = (ROOT / "deploy" / "aws-config.example").read_text(encoding="utf-8")
    assert "<ACCOUNT_ID>" in config and "credential_source = Ec2InstanceMetadata" in config
    assert "aws_access_key_id" not in config and "aws_secret_access_key" not in config

    trust = json.loads((ROOT / "aws" / "role-trust-policy.example.json").read_text(encoding="utf-8"))
    principal = trust["Statement"][0]["Principal"]["AWS"]
    assert principal != "*" and "<ACCOUNT_ID>" in principal and trust["Statement"][0]["Action"] == "sts:AssumeRole"

    host = json.loads((ROOT / "aws" / "host-assume-role-policy.example.json").read_text(encoding="utf-8"))
    statement = host["Statement"][0]
    assert statement["Resource"].endswith(":role/ivrm-ops-agent") and "*" not in statement["Resource"]
    assert statement["Action"] == "sts:AssumeRole"


def test_no_real_identifiers_are_committed() -> None:
    for path in list((ROOT / "deploy").glob("*")) + list((ROOT / "aws").glob("*.json")) + [ROOT / "DEPLOY.md"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b\d{12}\b", text), path.name  # AWS account ids
        assert not re.search(r"\bmi-[0-9a-f]{17}\b|\bi-[0-9a-f]{17}\b", text), path.name
