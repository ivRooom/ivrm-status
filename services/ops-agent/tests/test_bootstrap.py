from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("bootstrap", Path(__file__).resolve().parents[1] / "aws" / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)

ACCOUNT = "123456789012"
HOST = "ivrm-ec2-role"
HOST_ARN = f"arn:aws:iam::{ACCOUNT}:role/{HOST}"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/ivrm-ops-agent"
PROFILE_ARN = f"arn:aws:bedrock:ap-northeast-1:{ACCOUNT}:inference-profile/jp.anthropic.claude-haiku-4-5-20251001-v1:0"
MODELS = [
    "arn:aws:bedrock:ap-northeast-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    "arn:aws:bedrock:ap-northeast-3::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
]
TRUST = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"AWS": HOST_ARN}, "Action": "sts:AssumeRole"}],
}


class FakeAws:
    """Stands in for the aws CLI. It records every command; reads answer from `self.*`."""

    def __init__(self, arn=f"arn:aws:iam::{ACCOUNT}:user/admin", account=ACCOUNT) -> None:
        self.calls: list[list[str]] = []
        self.identity = {"Account": account, "Arn": arn}
        self.roles = {HOST: {"Arn": HOST_ARN}}
        self.budgets: list[str] = []
        self.simulate_answer = self.intended
        self.simulate_calls = 0

    def __call__(self, argv):
        self.calls.append(argv)
        service, op = argv[1], argv[2]
        arg = lambda flag: argv[argv.index(flag) + 1]  # noqa: E731
        if (service, op) == ("sts", "get-caller-identity"):
            return self.identity
        if (service, op) == ("iam", "get-role"):
            name = arg("--role-name")
            if name not in self.roles:
                raise bootstrap.Failed("An error occurred (NoSuchEntity) when calling the GetRole operation")
            return {"Role": self.roles[name]}
        if (service, op) == ("bedrock", "get-inference-profile"):
            return {"inferenceProfileArn": PROFILE_ARN, "models": [{"modelArn": m} for m in MODELS]}
        if (service, op) == ("iam", "create-role"):
            self.roles[arg("--role-name")] = {"Arn": ROLE_ARN, "AssumeRolePolicyDocument": json.loads(arg("--assume-role-policy-document"))}
            return {}
        if (service, op) == ("iam", "put-role-policy"):
            return None
        if (service, op) == ("budgets", "describe-budgets"):
            return {"Budgets": [{"BudgetName": n} for n in self.budgets]}
        if (service, op) == ("budgets", "create-budget"):
            self.budgets.append(json.loads(arg("--budget"))["BudgetName"])
            return None
        if (service, op) == ("iam", "simulate-principal-policy"):
            self.simulate_calls += 1
            context = None
            if "--context-entries" in argv:
                context = arg("--context-entries")
            decision = self.simulate_answer(arg("--action-names"), arg("--resource-arns"), context)
            return {"EvaluationResults": [{"EvalDecision": decision}]}
        raise AssertionError(f"unexpected command {argv}")

    @staticmethod
    def intended(action, resource, context):
        # what the policies we create really grant
        if action == "sts:AssumeRole":
            return "allowed"
        if action == "bedrock:InvokeModel" and resource == PROFILE_ARN:
            return "allowed"
        if action == "bedrock:InvokeModel" and resource in MODELS and context is not None:
            return "allowed"
        return "implicitDeny"

    def mutations(self):
        return [c for c in self.calls if (c[1], c[2]) in {("iam", "create-role"), ("iam", "put-role-policy"), ("budgets", "create-budget")}]


def run(aws, *extra, host=HOST, email="me@example.com"):
    argv = ["--account-id", ACCOUNT, "--host-role", host, "--email", email, *extra]
    return bootstrap.main(argv, runner=aws, sleep=lambda _s: None)


def test_without_apply_nothing_is_changed_and_the_plan_is_printed(capsys) -> None:
    aws = FakeAws()
    assert run(aws) == 0
    assert aws.mutations() == []
    out = capsys.readouterr().out
    assert "Nothing was changed" in out and "create role ivrm-ops-agent" in out and "create budget" in out


def test_apply_creates_the_role_the_policies_and_the_budget_in_order() -> None:
    aws = FakeAws()
    assert run(aws, "--apply") == 0
    kinds = [(c[1], c[2], c[c.index("--role-name") + 1] if "--role-name" in c else None) for c in aws.mutations()]
    assert kinds == [
        ("iam", "create-role", "ivrm-ops-agent"),
        ("iam", "put-role-policy", "ivrm-ops-agent"),
        ("iam", "put-role-policy", HOST),
        ("budgets", "create-budget", None),
    ]


def test_the_trust_policy_names_only_the_host_role() -> None:
    aws = FakeAws()
    run(aws, "--apply")
    create = next(c for c in aws.calls if c[2] == "create-role")
    trust = json.loads(create[create.index("--assume-role-policy-document") + 1])
    assert [s["Principal"] for s in trust["Statement"]] == [{"AWS": HOST_ARN}]


def test_the_host_policy_allows_assuming_only_the_ops_agent_role() -> None:
    aws = FakeAws()
    run(aws, "--apply")
    put = next(c for c in aws.calls if c[2] == "put-role-policy" and c[c.index("--role-name") + 1] == HOST)
    document = json.loads(put[put.index("--policy-document") + 1])
    assert document["Statement"][0]["Action"] == "sts:AssumeRole" and document["Statement"][0]["Resource"] == ROLE_ARN
    assert "*" not in json.dumps(document)


def test_the_role_policy_is_the_generated_least_privilege_one() -> None:
    aws = FakeAws()
    run(aws, "--apply")
    put = next(c for c in aws.calls if c[2] == "put-role-policy" and c[c.index("--role-name") + 1] == "ivrm-ops-agent")
    text = put[put.index("--policy-document") + 1]
    assert "*" not in text and json.loads(text)["Statement"][0]["Resource"] == PROFILE_ARN


def test_the_budget_has_three_email_alerts_and_no_service_filter_by_default() -> None:
    aws = FakeAws()
    run(aws, "--apply")
    create = next(c for c in aws.calls if c[2] == "create-budget")
    budget = json.loads(create[create.index("--budget") + 1])
    notes = json.loads(create[create.index("--notifications-with-subscribers") + 1])
    assert budget["BudgetLimit"] == {"Amount": "10", "Unit": "USD"} and "CostFilters" not in budget
    assert [(n["Notification"]["NotificationType"], n["Notification"]["Threshold"]) for n in notes] == [
        ("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100)]
    assert all(n["Subscribers"] == [{"SubscriptionType": "EMAIL", "Address": "me@example.com"}] for n in notes)


def test_a_service_filter_is_passed_through() -> None:
    aws = FakeAws()
    run(aws, "--apply", "--service-filter", "Amazon Bedrock")
    create = next(c for c in aws.calls if c[2] == "create-budget")
    assert json.loads(create[create.index("--budget") + 1])["CostFilters"] == {"Service": ["Amazon Bedrock"]}


def test_running_it_twice_changes_nothing_the_second_time() -> None:
    aws = FakeAws()
    run(aws, "--apply")
    aws.calls.clear()
    assert run(aws, "--apply") == 0
    kinds = [(c[1], c[2]) for c in aws.mutations()]
    assert ("iam", "create-role") not in kinds and ("budgets", "create-budget") not in kinds


def test_an_existing_role_with_a_different_trust_policy_is_refused_untouched(capsys) -> None:
    aws = FakeAws()
    aws.roles["ivrm-ops-agent"] = {
        "Arn": ROLE_ARN,
        "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}, "Action": "sts:AssumeRole"}]},
    }
    assert run(aws, "--apply") == 2
    assert aws.mutations() == [] and "DIFFERENT trust policy" in capsys.readouterr().err


def test_an_existing_matching_role_is_accepted_even_if_aws_returns_a_single_statement_object() -> None:
    aws = FakeAws()
    aws.roles["ivrm-ops-agent"] = {
        "Arn": ROLE_ARN,
        "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": {"Sid": "x", **TRUST["Statement"][0]}},
    }
    assert run(aws, "--apply") == 0


def test_an_existing_budget_is_left_alone() -> None:
    aws = FakeAws()
    aws.budgets = ["ivrm-ops-agent-monthly"]
    run(aws, "--apply")
    assert not any(c[2] == "create-budget" for c in aws.calls)


def test_credentials_of_another_account_are_refused() -> None:
    aws = FakeAws(account="999999999999")
    assert run(aws, "--apply") == 2 and aws.mutations() == []


def test_root_credentials_are_refused_unless_explicitly_allowed() -> None:
    aws = FakeAws(arn=f"arn:aws:iam::{ACCOUNT}:root")
    assert run(aws, "--apply") == 2 and aws.mutations() == []
    assert run(FakeAws(arn=f"arn:aws:iam::{ACCOUNT}:root"), "--apply", "--allow-root") == 0


def test_a_missing_host_role_stops_before_any_change() -> None:
    aws = FakeAws()
    assert run(aws, "--apply", host="no-such-role") == 3
    assert aws.mutations() == []


def test_the_host_role_cannot_be_the_agent_role_itself() -> None:
    aws = FakeAws()
    aws.roles["ivrm-ops-agent"] = {"Arn": ROLE_ARN}
    assert run(aws, "--apply", host="ivrm-ops-agent") == 2 and aws.mutations() == []


@pytest.mark.parametrize("extra", [["--budget-usd", "0"], ["--budget-usd", "5000"]])
def test_an_unreasonable_budget_is_refused(extra) -> None:
    aws = FakeAws()
    assert run(aws, *extra) == 2 and aws.calls == []


@pytest.mark.parametrize("email", ["", "not-an-email", "a b@example.com", "x@y"])
def test_a_bad_email_is_refused(email) -> None:
    aws = FakeAws()
    assert run(aws, email=email) == 2 and aws.calls == []


def test_a_bad_account_id_is_refused() -> None:
    assert bootstrap.main(["--account-id", "123", "--host-role", HOST, "--email", "a@b.co"], runner=FakeAws()) == 2


def test_a_wrong_verification_result_fails_loudly(capsys) -> None:
    aws = FakeAws()
    aws.simulate_answer = lambda action, resource, context: "allowed"  # even the denials come back allowed
    assert run(aws, "--apply") == 3
    assert "[FAIL] the role may NOT invoke that model directly" in capsys.readouterr().out
    assert aws.simulate_calls > 6  # it retried before giving up


def test_verification_checks_the_intended_boundaries() -> None:
    aws = FakeAws()
    seen = []

    def answer(action, resource, context):
        seen.append((action, resource, context is not None))
        denied = (action == "iam:CreateUser") or (action == "bedrock:InvokeModel" and context is None and resource in MODELS) or "sonnet" in resource
        return "implicitDeny" if denied else "allowed"

    aws.simulate_answer = answer
    assert run(aws, "--apply") == 0
    actions = {s[0] for s in seen}
    assert {"bedrock:InvokeModel", "iam:CreateUser", "sts:AssumeRole"} <= actions


def test_it_never_creates_access_keys_or_users() -> None:
    aws = FakeAws()
    run(aws, "--apply")
    assert not any(c[2] in {"create-access-key", "create-user", "attach-role-policy"} for c in aws.calls)


def test_show_costs_is_read_only_and_sorted(capsys) -> None:
    aws = FakeAws()

    def ce(argv):
        if argv[1] == "ce":
            return {"ResultsByTime": [{"Groups": [
                {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "1.5"}}},
                {"Keys": ["Claude Haiku 4.5 (Amazon Bedrock Edition)"], "Metrics": {"UnblendedCost": {"Amount": "0.02"}}},
            ]}]}
        return aws(argv)

    assert bootstrap.main(["--account-id", ACCOUNT, "--show-costs"], runner=ce) == 0
    out = capsys.readouterr().out
    assert out.index("Amazon EC2") < out.index("Bedrock Edition")
    assert aws.mutations() == []
