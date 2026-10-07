"""The shipped configs, SSM document and IAM policy example must stay safe and consistent."""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops_gate.gate import load_config  # noqa: E402

CONFIGS = {"oci": ROOT / "config.oci.example.json", "lightsail": ROOT / "config.lightsail.example.json"}
EXPECTED_OPS = {"oci": {"start_mc_resource", "restart_mc_main"}, "lightsail": {"restart_herta"}}
SECRET = b"0123456789abcdef" * 4


def load_example(name: str, tmp_path: Path):
    raw = json.loads(CONFIGS[name].read_text(encoding="utf-8"))
    secret = tmp_path / "secret"
    secret.write_bytes(SECRET)
    secret.chmod(0o600)
    raw["ticket_secret_file"] = str(secret)
    raw["approver_discord_ids"] = ["123456789012345678"]  # the example holds a placeholder
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return raw, load_config(path)


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_each_host_config_loads_and_enables_exactly_its_operations(name: str, tmp_path: Path) -> None:
    _, config = load_example(name, tmp_path)
    assert set(config.operations) == EXPECTED_OPS[name]


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_configs_never_contain_a_real_approver_id(name: str) -> None:
    text = CONFIGS[name].read_text(encoding="utf-8")
    assert "REPLACE_WITH_APPROVER_DISCORD_USER_ID" in text
    assert not re.search(r"\b\d{17,20}\b", text)


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_configured_commands_are_the_reviewed_scripts_only(name: str, tmp_path: Path) -> None:
    raw, _ = load_example(name, tmp_path)
    for spec in raw["operations"].values():
        script = spec["argv"][1]
        assert spec["argv"][0] == "/bin/sh" and script.startswith("/opt/ivrm-ops-gate/ops/")
        assert (ROOT / "ops" / Path(script).name).is_file()
        assert "sudo" not in " ".join(spec["argv"])  # the gate itself runs with the needed rights


def test_restarting_the_main_server_is_guarded_by_the_no_players_precheck(tmp_path: Path) -> None:
    raw, config = load_example("oci", tmp_path)
    precheck = config.operations["restart_mc_main"].precheck_argv
    assert precheck and precheck[1].endswith("precheck_no_players.py") and precheck[2].startswith("https://")
    assert "precheck_argv" not in raw["operations"]["start_mc_resource"]  # starting is not disruptive
    assert (ROOT / "ops" / "precheck_no_players.py").is_file()


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_every_operation_has_a_cooldown_and_a_timeout_longer_than_the_script_waits(name: str, tmp_path: Path) -> None:
    waits = {"start_mc_resource": 180, "restart_mc_main": 300, "restart_herta": 120 + 120}
    _, config = load_example(name, tmp_path)
    for operation in config.operations.values():
        assert operation.cooldown_seconds >= 300
        assert operation.timeout_seconds > waits[operation.name]


# --- SSM document ---------------------------------------------------------------------

DOCUMENT = json.loads((ROOT / "aws" / "ssm-document.json").read_text(encoding="utf-8"))
INJECTIONS = ["a'; id #", "a b", "$(id)", "`id`", "a\nb", "a|b", "a&b", "a;b", "", "a'b", 'a"b', "../x", "a\\b"]


def test_the_ssm_document_runs_only_the_gate_with_validated_parameters() -> None:
    steps = DOCUMENT["mainSteps"]
    assert len(steps) == 1 and steps[0]["action"] == "aws:runShellScript"
    commands = steps[0]["inputs"]["runCommand"]
    assert len(commands) == 1
    command = commands[0]
    assert command.endswith("/opt/ivrm-ops-gate/run.sh")
    assert set(re.findall(r"\{\{\s*(\w+)\s*\}\}", command)) == set(DOCUMENT["parameters"]) == {"verb", "ticket", "signature"}
    # Every substituted value sits inside one pair of single quotes, so a value without a quote
    # character cannot leave it.
    assert command.startswith("SSH_ORIGINAL_COMMAND='") and command.count("'") == 2


@pytest.mark.parametrize("parameter", ["ticket", "signature"])
def test_ssm_parameter_patterns_reject_shell_metacharacters(parameter: str) -> None:
    pattern = re.compile(DOCUMENT["parameters"][parameter]["allowedPattern"])
    for bad in INJECTIONS:
        assert not pattern.fullmatch(bad), (parameter, bad)


def test_ssm_parameter_patterns_accept_real_values() -> None:
    from ops_gate.gate import sign_ticket

    now = 1_800_000_000
    ticket, signature = sign_ticket(
        SECRET,
        {"v": 1, "op": "restart_herta", "proposal_id": "prop-00000001", "proposal_hash": "a" * 64,
         "approver_discord_id": "123456789012345678", "issued_at": now, "expires_at": now + 600, "nonce": "n" * 24},
    )
    assert re.fullmatch(DOCUMENT["parameters"]["ticket"]["allowedPattern"], ticket)
    assert re.fullmatch(DOCUMENT["parameters"]["signature"]["allowedPattern"], signature)
    assert DOCUMENT["parameters"]["verb"]["allowedValues"] == ["run", "check"]


def test_the_rendered_command_cannot_be_split_into_extra_commands() -> None:
    command = DOCUMENT["mainSteps"][0]["inputs"]["runCommand"][0]
    ticket_re = re.compile(DOCUMENT["parameters"]["ticket"]["allowedPattern"])
    worst_ticket = "A" * 4096
    assert ticket_re.fullmatch(worst_ticket)
    rendered = re.sub(r"\{\{\s*verb\s*\}\}", "run", command)
    rendered = re.sub(r"\{\{\s*ticket\s*\}\}", worst_ticket, rendered)
    rendered = re.sub(r"\{\{\s*signature\s*\}\}", "0" * 64, rendered)
    words = shlex.split(rendered)
    assert [w for w in words if "=" not in w] == ["/opt/ivrm-ops-gate/run.sh"]  # one env assignment, one program
    assert words[0] == f"SSH_ORIGINAL_COMMAND=run {worst_ticket} {'0' * 64}"


# --- IAM policy example ---------------------------------------------------------------

POLICY = json.loads((ROOT / "aws" / "caller-policy.example.json").read_text(encoding="utf-8"))


def test_the_caller_can_only_send_the_gate_document_to_listed_hosts() -> None:
    send = [s for s in POLICY["Statement"] if s["Action"] == "ssm:SendCommand"]
    resources = [r for s in send for r in ([s["Resource"]] if isinstance(s["Resource"], str) else s["Resource"])]
    assert all("*" not in r for r in resources)
    assert any(r.endswith(":document/ivrm-ops-gate-run") for r in resources)
    assert not any("AWS-RunShellScript" in r or "document/AWS-" in r for r in resources)
    assert all(s["Effect"] == "Allow" for s in POLICY["Statement"])
    actions = {s["Action"] for s in POLICY["Statement"]}
    assert actions == {"ssm:SendCommand", "ssm:GetCommandInvocation"}  # nothing like StartSession or ssm:*


def test_the_document_name_in_the_policy_matches_the_documented_name() -> None:
    assert "ivrm-ops-gate-run" in json.dumps(POLICY)
