#!/usr/bin/env python3
"""Create the ops-agent IAM role and the monthly AWS Budgets alert.

Run it yourself, with your own administrator credentials (for example in AWS CloudShell). It is
NOT meant to be run with the account root.

    python aws/bootstrap.py --account-id 123456789012 --host-role <instance-profile-role> \\
        --email you@example.com                 # plan only: reads state, prints what it would do
    python aws/bootstrap.py ... --apply         # make the changes

    python aws/bootstrap.py --account-id ... --show-costs   # last 14 days by service (read-only)

What --apply does, in order (each step is checked first, so running it again is safe):

  1. refuses unless the credentials belong to --account-id and are not the root user
  2. creates the role `ivrm-ops-agent`, trusted ONLY by the host's instance role
  3. gives that role InvokeModel on the one inference profile (and its destination models, only
     when reached through that profile), generated from the profile itself by make_policy.py
  4. lets the host role assume ONLY that role (an inline policy added to the host role)
  5. creates the monthly budget with 80% / 100% / forecast-100% email alerts
  6. checks the result with iam:SimulatePrincipalPolicy, without calling the model

It never creates an access key and never changes anything that already exists in a different shape:
if the role or the budget exists differently, it stops and says so. Python 3.9+, standard library.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROLE_NAME = "ivrm-ops-agent"
ROLE_POLICY_NAME = "ops-agent-bedrock-invoke"
HOST_POLICY_NAME = "ivrm-ops-agent-assume"
BUDGET_NAME = "ivrm-ops-agent-monthly"
DEFAULT_PROFILE = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

_ACCOUNT = re.compile(r"\d{12}")
_ROLE = re.compile(r"[A-Za-z0-9+=,.@_-]{1,64}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
_SERVICE = re.compile(r"[\w ().,&/+:'-]{1,120}")


class Refused(Exception):
    """A safety check failed. Nothing was changed."""


class Failed(Exception):
    """A command failed or returned something unexpected."""


Runner = Callable[[list[str]], Any]


def real_runner(argv: list[str]) -> Any:
    """Run an `aws ...` command (a list: no shell) and return its parsed JSON, if any."""
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    if completed.returncode != 0:
        raise Failed(f"{' '.join(argv[:4])} failed: {completed.stderr.strip()[:400]}")
    text = completed.stdout.strip()
    return json.loads(text) if text else None


def _load_make_policy():
    spec = importlib.util.spec_from_file_location("make_policy", Path(__file__).with_name("make_policy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, sort_keys=True)


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Bootstrap:
    def __init__(self, args: argparse.Namespace, runner: Runner, sleep: Callable[[float], None] = time.sleep) -> None:
        self.args = args
        self.run = runner
        self.sleep = sleep
        self.mutations: list[str] = []

    # --- output -------------------------------------------------------------------------------

    def say(self, tag: str, text: str) -> None:
        print(f"[{tag}] {text}")

    def mutate(self, description: str, argv: list[str]) -> Any:
        """A command that changes something: executed only with --apply, otherwise just shown."""
        self.mutations.append(description)
        if not self.args.apply:
            self.say("plan", description)
            return None
        self.say("run", description)
        return self.run(argv)

    # --- pieces ---------------------------------------------------------------------------------

    def preflight(self) -> None:
        identity = self.run(["aws", "sts", "get-caller-identity", "--output", "json"])
        if not isinstance(identity, dict) or identity.get("Account") != self.args.account_id:
            raise Refused(f"these credentials belong to account {identity.get('Account') if isinstance(identity, dict) else '?'}, not {self.args.account_id}")
        arn = str(identity.get("Arn", ""))
        if arn.endswith(":root") and not self.args.allow_root:
            raise Refused("these are the account root credentials; use an administrator user or role (or pass --allow-root if you really must)")
        self.say("ok", f"account {self.args.account_id}, as {arn.split(':', 5)[-1]}")

    def host_role_arn(self) -> str:
        found = self.run(["aws", "iam", "get-role", "--role-name", self.args.host_role, "--output", "json"])
        role = found.get("Role", {}) if isinstance(found, dict) else {}
        arn = role.get("Arn")
        if not arn or ":role/" not in arn:
            raise Failed(f"the host role {self.args.host_role} was not found")
        if self.args.host_role == ROLE_NAME:
            raise Refused("the host role must not be the ops-agent role itself")
        self.say("ok", f"host role {self.args.host_role}")
        return str(arn)

    def permissions_policy(self) -> tuple[dict[str, Any], str]:
        profile = self.run(
            ["aws", "bedrock", "get-inference-profile", "--inference-profile-identifier", self.args.profile_id,
             "--region", self.args.region, "--output", "json"]
        )
        try:
            policy = _load_make_policy().build_policy(profile)
        except ValueError as exc:
            raise Refused(f"cannot build a safe policy from the inference profile: {exc}") from None
        self.say("ok", f"policy for {self.args.profile_id}: InvokeModel on the profile and its {len(policy['Statement'][1]['Resource'])} destination model(s)")
        return policy, str(profile["inferenceProfileArn"])

    def ensure_role(self, host_arn: str) -> str:
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "OnlyTheOpsAgentHostRole", "Effect": "Allow", "Principal": {"AWS": host_arn}, "Action": "sts:AssumeRole"}
            ],
        }
        role_arn = f"arn:aws:iam::{self.args.account_id}:role/{ROLE_NAME}"
        try:
            existing = self.run(["aws", "iam", "get-role", "--role-name", ROLE_NAME, "--output", "json"])
        except Failed as exc:
            if "NoSuchEntity" not in str(exc):
                raise
            existing = None
        if existing:
            document = existing["Role"].get("AssumeRolePolicyDocument", {})
            if _canon(_strip_sid(document)) != _canon(_strip_sid(trust)):
                raise Refused(f"the role {ROLE_NAME} already exists with a DIFFERENT trust policy; review it by hand, nothing was changed")
            self.say("skip", f"role {ROLE_NAME} already exists with the expected trust policy")
            return role_arn
        self.mutate(
            f"create role {ROLE_NAME}, trusted only by {host_arn.split('/')[-1]}",
            ["aws", "iam", "create-role", "--role-name", ROLE_NAME, "--assume-role-policy-document", _json(trust),
             "--description", "ivrm-status ops-agent: invoke one Bedrock inference profile",
             "--max-session-duration", "3600", "--tags", "Key=project,Value=ivrm-status", "Key=owner,Value=ops-agent",
             "--output", "json"],
        )
        return role_arn

    def ensure_role_policy(self, policy: dict[str, Any]) -> None:
        self.mutate(
            f"attach inline policy {ROLE_POLICY_NAME} to {ROLE_NAME} (InvokeModel on the profile only)",
            ["aws", "iam", "put-role-policy", "--role-name", ROLE_NAME, "--policy-name", ROLE_POLICY_NAME,
             "--policy-document", _json(policy)],
        )

    def ensure_host_policy(self) -> None:
        document = {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "AssumeOnlyTheOpsAgentRole", "Effect": "Allow", "Action": "sts:AssumeRole",
                 "Resource": f"arn:aws:iam::{self.args.account_id}:role/{ROLE_NAME}"}
            ],
        }
        self.mutate(
            f"add inline policy {HOST_POLICY_NAME} to the HOST role {self.args.host_role} (sts:AssumeRole on {ROLE_NAME} only; "
            "this is the one change to an existing production role)",
            ["aws", "iam", "put-role-policy", "--role-name", self.args.host_role, "--policy-name", HOST_POLICY_NAME,
             "--policy-document", _json(document)],
        )

    def ensure_budget(self) -> None:
        listed = self.run(["aws", "budgets", "describe-budgets", "--account-id", self.args.account_id, "--output", "json"])
        names = [b.get("BudgetName") for b in (listed or {}).get("Budgets", [])] if isinstance(listed, dict) else []
        if BUDGET_NAME in names:
            self.say("skip", f"budget {BUDGET_NAME} already exists (not modified)")
            return
        budget: dict[str, Any] = {
            "BudgetName": BUDGET_NAME,
            "BudgetType": "COST",
            "TimeUnit": "MONTHLY",
            "BudgetLimit": {"Amount": f"{self.args.budget_usd:g}", "Unit": "USD"},
        }
        if self.args.service_filter:
            budget["CostFilters"] = {"Service": list(self.args.service_filter)}
        subscriber = [{"SubscriptionType": "EMAIL", "Address": self.args.email}]
        notifications = [
            {"Notification": {"NotificationType": kind, "ComparisonOperator": "GREATER_THAN", "Threshold": threshold,
                              "ThresholdType": "PERCENTAGE"}, "Subscribers": subscriber}
            for kind, threshold in (("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100))
        ]
        scope = f"services {list(self.args.service_filter)}" if self.args.service_filter else "the whole account"
        self.mutate(
            f"create budget {BUDGET_NAME}: {budget['BudgetLimit']['Amount']} USD / month for {scope}, "
            "email at 80% and 100% actual and 100% forecast (Budgets only notifies; it does not stop anything)",
            ["aws", "budgets", "create-budget", "--account-id", self.args.account_id, "--budget", _json(budget),
             "--notifications-with-subscribers", _json(notifications)],
        )

    # --- verification without calling the model ---------------------------------------------------------

    def simulate(self, role_arn: str, action: str, resource: str, context: Optional[str] = None) -> str:
        argv = ["aws", "iam", "simulate-principal-policy", "--policy-source-arn", role_arn, "--action-names", action,
                "--resource-arns", resource, "--output", "json"]
        if context:
            argv += ["--context-entries", f"ContextKeyName=bedrock:InferenceProfileArn,ContextKeyValues={context},ContextKeyType=string"]
        result = self.run(argv)
        decisions = [r.get("EvalDecision") for r in (result or {}).get("EvaluationResults", [])]
        return decisions[0] if decisions else "unknown"

    def verify(self, role_arn: str, host_arn: str, profile_arn: str, model_arns: list[str]) -> None:
        model = model_arns[0]
        other_model = model.rsplit("/", 1)[0] + "/anthropic.claude-sonnet-4-5-20250929-v1:0"
        checks = [
            ("the role may invoke the profile", role_arn, "bedrock:InvokeModel", profile_arn, None, "allowed"),
            ("the role may invoke the destination model through the profile", role_arn, "bedrock:InvokeModel", model, profile_arn, "allowed"),
            ("the role may NOT invoke that model directly", role_arn, "bedrock:InvokeModel", model, None, "denied"),
            ("the role may NOT invoke any other model", role_arn, "bedrock:InvokeModel", other_model, profile_arn, "denied"),
            ("the role may NOT do anything else (iam:CreateUser)", role_arn, "iam:CreateUser", "*", None, "denied"),
            ("the host role may assume the ops-agent role", host_arn, "sts:AssumeRole", role_arn, None, "allowed"),
        ]
        failures = []
        for label, source, action, resource, context, expected in checks:
            decision = "unknown"
            for attempt in range(4):  # IAM changes take a few seconds to be visible
                decision = self.simulate(source, action, resource, context)
                good = decision == "allowed" if expected == "allowed" else decision in {"implicitDeny", "explicitDeny"}
                if good:
                    break
                self.sleep(3)
            ok = decision == "allowed" if expected == "allowed" else decision in {"implicitDeny", "explicitDeny"}
            self.say("ok" if ok else "FAIL", f"{label}: {decision}")
            if not ok:
                failures.append(label)
        if failures:
            raise Failed("the permissions are not what was intended: " + "; ".join(failures))

    # --- the whole thing ------------------------------------------------------------------------------------

    def execute(self) -> None:
        self.preflight()
        host_arn = self.host_role_arn()
        policy, profile_arn = self.permissions_policy()
        role_arn = self.ensure_role(host_arn)
        self.ensure_role_policy(policy)
        self.ensure_host_policy()
        self.ensure_budget()
        if not self.args.apply:
            print(f"\nNothing was changed. {len(self.mutations)} change(s) are planned above; add --apply to make them.")
            print("The permissions policy that would be attached:\n" + json.dumps(policy, indent=2))
            return
        self.verify(role_arn, host_arn, profile_arn, policy["Statement"][1]["Resource"])
        print(f"\nDone: {len(self.mutations)} change(s). Next: run the ops-agent --check on the host (see DEPLOY.md).")


def _strip_sid(document: dict[str, Any]) -> dict[str, Any]:
    """Compare trust policies by meaning: the Sid is a label, and AWS may return a single statement
    as an object or a list."""
    statements = document.get("Statement", [])
    statements = [statements] if isinstance(statements, dict) else statements
    return {
        "Version": document.get("Version"),
        "Statement": [{k: v for k, v in s.items() if k != "Sid"} for s in statements],
    }


def show_costs(args: argparse.Namespace, runner: Runner, today: Optional[datetime.date] = None) -> int:
    today = today or datetime.date.today()
    start = (today - datetime.timedelta(days=14)).isoformat()
    end = (today + datetime.timedelta(days=1)).isoformat()
    result = runner(
        ["aws", "ce", "get-cost-and-usage", "--region", "us-east-1", "--time-period", f"Start={start},End={end}",
         "--granularity", "MONTHLY", "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE",
         "--output", "json"]
    )
    totals: dict[str, float] = {}
    for period in (result or {}).get("ResultsByTime", []):
        for group in period.get("Groups", []):
            totals[group["Keys"][0]] = totals.get(group["Keys"][0], 0.0) + float(group["Metrics"]["UnblendedCost"]["Amount"])
    print("Cost by service, last 14 days (USD). Use the exact name with --service-filter if you want the budget limited to it:")
    for name, amount in sorted(totals.items(), key=lambda item: -item[1]):
        print(f"  {amount:10.4f}  {name}")
    return 0


def parse(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-id", required=True, help="the 12 digit account id; the credentials must belong to it")
    parser.add_argument("--host-role", help="name of the instance profile role of the host that runs ops-agent")
    parser.add_argument("--email", help="where the budget alerts go")
    parser.add_argument("--budget-usd", type=float, default=10.0, help="monthly budget in USD (about 1500 JPY)")
    parser.add_argument("--service-filter", action="append", default=[], help="limit the budget to this exact service name (repeatable); default is the whole account")
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default="ap-northeast-1")
    parser.add_argument("--apply", action="store_true", help="make the changes (without it, nothing is changed)")
    parser.add_argument("--allow-root", action="store_true", help="allow the account root credentials (not recommended)")
    parser.add_argument("--show-costs", action="store_true", help="print the last 14 days of cost by service and exit (read-only)")
    return parser.parse_args(argv)


def validate(args: argparse.Namespace) -> None:
    if not _ACCOUNT.fullmatch(args.account_id):
        raise Refused("--account-id must be 12 digits")
    if args.show_costs:
        return
    if not args.host_role or not _ROLE.fullmatch(args.host_role):
        raise Refused("--host-role is required (the name of the instance profile role, not its ARN)")
    if not args.email or not _EMAIL.fullmatch(args.email):
        raise Refused("--email is required and must be a valid address")
    if not 1 <= args.budget_usd <= 1000:
        raise Refused("--budget-usd must be between 1 and 1000")
    for name in args.service_filter:
        if not _SERVICE.fullmatch(name):
            raise Refused(f"unusual characters in --service-filter {name!r}")


def main(argv: Optional[list[str]] = None, runner: Optional[Runner] = None, sleep: Callable[[float], None] = time.sleep) -> int:
    args = parse(argv)
    run = runner or real_runner
    try:
        validate(args)
        if args.show_costs:
            Bootstrap(args, run, sleep).preflight()
            return show_costs(args, run)
        Bootstrap(args, run, sleep).execute()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Failed as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
