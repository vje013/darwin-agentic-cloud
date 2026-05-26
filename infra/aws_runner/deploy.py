#!/usr/bin/env python3
"""
darwin-runner AWS deployment orchestrator.

Idempotent boto3 script that provisions the darwin-runner Lambda
functions in every supported region. Does the same work CDK or
Terraform would do, but with zero new tooling deps — just boto3.

Resources created PER REGION:
  - ECR repo:     darwin-runner-python  (image tag: latest + digest)
  - ECR repo:     darwin-runner-node
  - Lambda fn:    darwin-runner-python-{region}
  - Lambda fn:    darwin-runner-node-{region}

Shared (global, created once):
  - IAM role:     darwin-runner-execution-role
                  attached: AWSLambdaBasicExecutionRole

Usage:
  AWS_PROFILE=darwin python infra/aws_runner/deploy.py \\
      --regions us-east-1,us-west-2,eu-west-1,ap-northeast-1

  AWS_PROFILE=darwin python infra/aws_runner/deploy.py \\
      --regions us-east-1 --dry-run

Idempotency: every resource is checked-then-created. Re-running this
script after a partial failure is safe — only missing resources
get created.

Build context: this script reads `runner.py`, `runner.mjs`, and the
two Dockerfiles from the same directory. The Docker daemon must be
running locally to build the images.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ============================================================================
# Constants
# ============================================================================

#: AWS regions to deploy into. Override via --regions.
DEFAULT_REGIONS = ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1"]

#: IAM execution role attached to every Lambda function.
ROLE_NAME = "darwin-runner-execution-role"

#: Lambda function naming templates.
FUNCTION_NAME_TEMPLATE = "darwin-runner-{language}-{region}"

#: ECR repo names. Same in every region.
ECR_REPO_PYTHON = "darwin-runner-python"
ECR_REPO_NODE = "darwin-runner-node"

#: Lambda configuration.
DEFAULT_MEMORY_MB = 1024
DEFAULT_TIMEOUT_SEC = 900  # Lambda max
DEFAULT_RESERVED_CONCURRENCY = None  # no reservation — share account pool

#: Trust policy for the execution role.
TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

#: Managed policy for basic Lambda execution (CloudWatch Logs).
BASIC_EXECUTION_POLICY_ARN = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"


# ============================================================================
# Pretty logging
# ============================================================================


def log(msg: str, *, indent: int = 0) -> None:
    prefix = "  " * indent
    print(f"{prefix}• {msg}", flush=True)


def section(title: str) -> None:
    print(f"\n══ {title} ══", flush=True)


# ============================================================================
# IAM role
# ============================================================================


def ensure_iam_role(iam_client) -> str:
    """Create the Lambda execution role if it doesn\'t exist. Return ARN."""
    section("IAM execution role")
    try:
        resp = iam_client.get_role(RoleName=ROLE_NAME)
        log(f"role exists: {resp['Role']['Arn']}")
        role_arn = resp["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        log(f"creating role {ROLE_NAME}")
        resp = iam_client.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Execution role for darwin-runner Lambda functions.",
        )
        role_arn = resp["Role"]["Arn"]

    # Always (re-)attach the basic-execution policy. Idempotent.
    iam_client.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn=BASIC_EXECUTION_POLICY_ARN,
    )
    log("attached AWSLambdaBasicExecutionRole", indent=1)

    # IAM is eventually consistent. After role creation, Lambda may
    # refuse to attach it for a few seconds. Wait if the role was
    # newly created (resp["Role"]["CreateDate"] is recent).
    return role_arn


# ============================================================================
# ECR
# ============================================================================


def ensure_ecr_repo(ecr_client, repo_name: str) -> str:
    """Ensure ECR repo exists. Return repository URI."""
    try:
        resp = ecr_client.describe_repositories(repositoryNames=[repo_name])
        uri = resp["repositories"][0]["repositoryUri"]
        log(f"ECR repo exists: {repo_name}", indent=1)
        return uri
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
    log(f"creating ECR repo: {repo_name}", indent=1)
    resp = ecr_client.create_repository(
        repositoryName=repo_name,
        imageScanningConfiguration={"scanOnPush": True},
        encryptionConfiguration={"encryptionType": "AES256"},
    )
    return resp["repository"]["repositoryUri"]


def ecr_docker_login(ecr_client, account_id: str, region: str) -> None:
    """`docker login` against the ECR registry for this region."""
    resp = ecr_client.get_authorization_token()
    token_b64 = resp["authorizationData"][0]["authorizationToken"]
    registry = resp["authorizationData"][0]["proxyEndpoint"]

    import base64

    token = base64.b64decode(token_b64).decode("utf-8")
    username, password = token.split(":", 1)

    proc = subprocess.run(
        ["docker", "login", "-u", username, "--password-stdin", registry],
        input=password,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker login failed for {registry}: {proc.stderr}")


# ============================================================================
# Build + push images
# ============================================================================


def build_and_push_image(
    *,
    dockerfile: Path,
    context_dir: Path,
    repo_uri: str,
    tag: str = "latest",
    platform: str = "linux/amd64",
) -> str:
    """Build a Docker image and push it. Return the digest."""
    image_full = f"{repo_uri}:{tag}"
    log(f"docker build {dockerfile.name} → {image_full}", indent=1)

    # Build for linux/amd64 — Lambda doesn\'t support arm64 for
    # container images on all base images yet, and amd64 is universal.
    proc = subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            platform,
            "-f",
            str(dockerfile),
            "-t",
            image_full,
            "--provenance=false",  # required for ECR
            str(context_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"docker build failed for {dockerfile.name}")

    log(f"docker push {image_full}", indent=1)
    proc = subprocess.run(
        ["docker", "push", image_full],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"docker push failed for {image_full}")

    # Get the image digest after push.
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image_full],
        capture_output=True,
        text=True,
        check=True,
    )
    digest_line = proc.stdout.strip()
    # Format: "{repo_uri}@sha256:abc..."
    digest = digest_line.split("@", 1)[1] if "@" in digest_line else "unknown"
    log(f"digest: {digest}", indent=2)
    return digest


# ============================================================================
# Lambda
# ============================================================================


def ensure_lambda_function(
    *,
    lambda_client,
    function_name: str,
    image_uri: str,
    role_arn: str,
    memory_mb: int = DEFAULT_MEMORY_MB,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> None:
    """Create or update the Lambda function to point at image_uri."""
    try:
        lambda_client.get_function(FunctionName=function_name)
        log(f"function exists: {function_name}", indent=1)
        # Update the image URI (idempotent on same digest).
        lambda_client.update_function_code(
            FunctionName=function_name,
            ImageUri=image_uri,
            Publish=False,
        )
        # Wait for code-update to finish before reading config.
        _wait_for_function_active(lambda_client, function_name)
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Role=role_arn,
            MemorySize=memory_mb,
            Timeout=timeout_sec,
        )
        log("updated to latest image", indent=2)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    log(f"creating function: {function_name}", indent=1)
    # Lambda often 4xx-s on first create with "role not assumable yet"
    # because IAM is eventually consistent. Retry a few times.
    last_err = None
    for attempt in range(10):
        try:
            lambda_client.create_function(
                FunctionName=function_name,
                PackageType="Image",
                Code={"ImageUri": image_uri},
                Role=role_arn,
                MemorySize=memory_mb,
                Timeout=timeout_sec,
                Description="darwin-runner: executes Darwin Agentic Cloud workloads.",
                Tags={
                    "darwin": "agentic-cloud",
                    "component": "runner",
                },
            )
            log("created", indent=2)
            _wait_for_function_active(lambda_client, function_name)
            return
        except ClientError as e:
            last_err = e
            code = e.response["Error"]["Code"]
            msg = e.response["Error"].get("Message", "")
            if "role" in msg.lower() or code == "InvalidParameterValueException":
                log(
                    f"role not yet assumable (attempt {attempt + 1}/10), waiting 6s",
                    indent=2,
                )
                time.sleep(6)
                continue
            raise
    raise RuntimeError(f"create_function failed: {last_err}")


def _wait_for_function_active(lambda_client, function_name: str) -> None:
    """Poll until the function is in Active state."""
    for _ in range(60):
        resp = lambda_client.get_function_configuration(FunctionName=function_name)
        state = resp.get("State")
        last_update = resp.get("LastUpdateStatus")
        if state == "Active" and last_update in (None, "Successful"):
            return
        if state == "Failed" or last_update == "Failed":
            raise RuntimeError(
                f"function {function_name} in failed state: "
                f"{resp.get('StateReason') or resp.get('LastUpdateStatusReason')}"
            )
        time.sleep(2)
    raise RuntimeError(f"function {function_name} did not become Active in time")


# ============================================================================
# Main per-region flow
# ============================================================================


def deploy_region(
    *,
    region: str,
    account_id: str,
    role_arn: str,
    context_dir: Path,
    dry_run: bool,
) -> dict:
    """Deploy darwin-runner to one region. Return summary dict."""
    section(f"region: {region}")
    summary = {
        "region": region,
        "ecr_python": None,
        "ecr_node": None,
        "lambda_python": None,
        "lambda_node": None,
        "image_python_digest": None,
        "image_node_digest": None,
    }
    if dry_run:
        log("--dry-run set, skipping all AWS mutations")
        return summary

    ecr = boto3.client("ecr", region_name=region)
    lam = boto3.client("lambda", region_name=region)

    # ECR repos
    py_repo_uri = ensure_ecr_repo(ecr, ECR_REPO_PYTHON)
    node_repo_uri = ensure_ecr_repo(ecr, ECR_REPO_NODE)
    summary["ecr_python"] = py_repo_uri
    summary["ecr_node"] = node_repo_uri

    # Docker login
    log("docker login to ECR", indent=1)
    ecr_docker_login(ecr, account_id, region)

    # Build + push both images
    py_digest = build_and_push_image(
        dockerfile=context_dir / "Dockerfile.runner-python",
        context_dir=context_dir,
        repo_uri=py_repo_uri,
    )
    summary["image_python_digest"] = py_digest

    node_digest = build_and_push_image(
        dockerfile=context_dir / "Dockerfile.runner-node",
        context_dir=context_dir,
        repo_uri=node_repo_uri,
    )
    summary["image_node_digest"] = node_digest

    # Lambda functions
    py_fn = FUNCTION_NAME_TEMPLATE.format(language="python", region=region)
    node_fn = FUNCTION_NAME_TEMPLATE.format(language="node", region=region)

    py_image_uri = f"{py_repo_uri}@{py_digest}"
    node_image_uri = f"{node_repo_uri}@{node_digest}"

    ensure_lambda_function(
        lambda_client=lam,
        function_name=py_fn,
        image_uri=py_image_uri,
        role_arn=role_arn,
    )
    summary["lambda_python"] = py_fn

    ensure_lambda_function(
        lambda_client=lam,
        function_name=node_fn,
        image_uri=node_image_uri,
        role_arn=role_arn,
    )
    summary["lambda_node"] = node_fn

    return summary


# ============================================================================
# Entrypoint
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy darwin-runner to AWS.")
    parser.add_argument(
        "--regions",
        default=",".join(DEFAULT_REGIONS),
        help=f"Comma-separated AWS regions. Default: {','.join(DEFAULT_REGIONS)}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip all AWS mutations — just print what would happen.",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS profile name. Defaults to AWS_PROFILE env var.",
    )
    args = parser.parse_args()

    regions = [r.strip() for r in args.regions.split(",") if r.strip()]
    if not regions:
        parser.error("--regions must not be empty")

    if args.profile:
        boto3.setup_default_session(profile_name=args.profile)

    context_dir = Path(__file__).resolve().parent

    section("preflight")
    sts = boto3.client("sts")
    ident = sts.get_caller_identity()
    account_id = ident["Account"]
    log(f"AWS account:   {account_id}")
    log(f"caller ARN:    {ident['Arn']}")
    log(f"regions:       {regions}")
    log(f"context dir:   {context_dir}")
    log(f"dry-run:       {args.dry_run}")

    iam = boto3.client("iam")
    role_arn = ensure_iam_role(iam) if not args.dry_run else "arn:aws:iam::dryrun:role/dry"

    summaries = []
    for region in regions:
        summaries.append(
            deploy_region(
                region=region,
                account_id=account_id,
                role_arn=role_arn,
                context_dir=context_dir,
                dry_run=args.dry_run,
            )
        )

    section("summary")
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
