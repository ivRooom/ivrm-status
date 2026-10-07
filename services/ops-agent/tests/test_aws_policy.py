from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("make_policy", Path(__file__).resolve().parents[1] / "aws" / "make_policy.py")
make_policy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(make_policy)

PROFILE_ARN = "arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0"
MODELS = [
    {"modelArn": "arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"},
    {"modelArn": "arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"},
]


def test_policy_allows_the_profile_and_each_destination_model_only_via_the_profile() -> None:
    policy = make_policy.build_policy({"inferenceProfileArn": PROFILE_ARN, "models": MODELS})
    first, second = policy["Statement"]
    assert first["Resource"] == PROFILE_ARN and first["Action"] == "bedrock:InvokeModel"
    assert second["Resource"] == sorted(item["modelArn"] for item in MODELS)
    assert second["Condition"] == {"StringEquals": {"bedrock:InferenceProfileArn": PROFILE_ARN}}


def test_policy_never_contains_a_wildcard_or_other_actions() -> None:
    text = json.dumps(make_policy.build_policy({"inferenceProfileArn": PROFILE_ARN, "models": MODELS}))
    assert "*" not in text
    assert set(
        action
        for statement in make_policy.build_policy({"inferenceProfileArn": PROFILE_ARN, "models": MODELS})["Statement"]
        for action in [statement["Action"]]
    ) == {"bedrock:InvokeModel"}


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"inferenceProfileArn": PROFILE_ARN},
        {"inferenceProfileArn": PROFILE_ARN, "models": []},
        {"inferenceProfileArn": "arn:aws:bedrock:*:*:inference-profile/*", "models": MODELS},
        {"inferenceProfileArn": PROFILE_ARN, "models": [{"modelArn": "arn:aws:bedrock:*::foundation-model/*"}]},
        {"inferenceProfileArn": PROFILE_ARN, "models": [{"modelArn": "*"}]},
    ],
)
def test_it_refuses_to_guess_or_widen(profile: dict) -> None:
    with pytest.raises(ValueError):
        make_policy.build_policy(profile)


def test_cli_works_offline_from_a_saved_profile(tmp_path: Path, capsys) -> None:
    saved = tmp_path / "profile.json"
    saved.write_text(json.dumps({"inferenceProfileArn": PROFILE_ARN, "models": MODELS}), encoding="utf-8")
    assert make_policy.main(["--from-file", str(saved)]) == 0
    assert json.loads(capsys.readouterr().out)["Statement"][0]["Resource"] == PROFILE_ARN


def test_cli_exits_non_zero_on_a_bad_profile(tmp_path: Path, capsys) -> None:
    saved = tmp_path / "profile.json"
    saved.write_text("{}", encoding="utf-8")
    assert make_policy.main(["--from-file", str(saved)]) == 2
    assert "error" in capsys.readouterr().err
