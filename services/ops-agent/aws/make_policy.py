"""Build the least-privilege IAM policy for ops-agent from a Bedrock inference profile.

Regional cross-region inference profiles (jp.*, apac.*, us.*) need permission on the profile
AND on the foundation-model ARN in every destination region. This reads the profile with
the caller's own AWS credentials (read-only: bedrock:GetInferenceProfile) and prints a
policy JSON. It changes nothing in AWS.

    python aws/make_policy.py                      # uses OPS_AGENT_BEDROCK_* settings
    python aws/make_policy.py --from-file profile.json   # offline, for review or tests
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

_PROFILE_ARN = re.compile(r"^arn:aws[a-z-]*:bedrock:[a-z0-9-]+:\d{12}:inference-profile/[A-Za-z0-9._:-]+$")
_MODEL_ARN = re.compile(r"^arn:aws[a-z-]*:bedrock:[a-z0-9-]+::foundation-model/[A-Za-z0-9._:-]+$")


def build_policy(profile: dict[str, Any]) -> dict[str, Any]:
    profile_arn = profile.get("inferenceProfileArn")
    if not isinstance(profile_arn, str) or not _PROFILE_ARN.match(profile_arn):
        raise ValueError("profile has no valid inferenceProfileArn")
    if "/global." in profile_arn:
        # Global profiles also need a regionless foundation-model ARN. That policy shape has not
        # been verified here, so refuse rather than emit one that may be wrong or too wide.
        raise ValueError("global.* profiles are not supported; use a regional profile such as jp.* or apac.*")
    items = profile.get("models")
    if not isinstance(items, list):
        raise ValueError("profile has no models list")
    models = [item.get("modelArn") for item in items if isinstance(item, dict)]
    if not models or not all(isinstance(arn, str) and _MODEL_ARN.match(arn) for arn in models):
        raise ValueError("profile has no valid foundation-model ARNs (refusing to guess a wildcard)")

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeThroughTheProfile",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": profile_arn,
            },
            {
                # The destination models may only be used *through* this profile.
                "Sid": "InvokeDestinationModelsOnlyViaTheProfile",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": sorted(set(models)),
                "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": profile_arn}},
            },
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OPS_AGENT_BEDROCK_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0"))
    parser.add_argument("--region", default=os.environ.get("OPS_AGENT_BEDROCK_REGION", "ap-northeast-1"))
    parser.add_argument("--from-file", help="read the GetInferenceProfile response from a JSON file instead of calling AWS")
    args = parser.parse_args(argv)

    if args.from_file:
        with open(args.from_file, encoding="utf-8") as handle:
            profile = json.load(handle)
    else:
        import boto3

        client = boto3.client("bedrock", region_name=args.region)
        profile = client.get_inference_profile(inferenceProfileIdentifier=args.profile)

    try:
        policy = build_policy(profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(policy, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
