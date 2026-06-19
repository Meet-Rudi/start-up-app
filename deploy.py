#!/usr/bin/env python3
"""
MEET_RUDI one-command deployer.

Usage (Windows CMD):
    python deploy.py <component>

Components:
    base      -> stack meetrudi-base    : shared S3 data bucket + meetrudi-lambda-runner role
    ask-ai    -> stack meetrudi-ask-ai  : meetrudi-ask-ai Lambda + Function URL
                                          (also seeds prompt/context/config files to S3)

Runs `sam build` (where needed) + `sam deploy` with every flag pre-filled. No manual steps.
Deploy `base` once before `ask-ai`.
"""

import os
import sys
import shutil
import subprocess

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
CAPABILITIES = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"]
ROOT = os.path.dirname(os.path.abspath(__file__))

COMPONENTS = {
    "base": {
        "stack": "meetrudi-base",
        "template": "infra/base/template.yaml",
        "build": False,  # plain CloudFormation, nothing to build
    },
    "ask-ai": {
        "stack": "meetrudi-ask-ai",
        "template": "services/ask-ai/template.yaml",
        "build": True,
        "seed_dir": "services/ask-ai/seed",
        "seed_bucket_from": "meetrudi-base",  # stack whose DataBucketName output is the target
    },
    "rudi-chat": {
        "stack": "meetrudi-rudi-chat",
        "template": "services/rudi-chat/template.yaml",
        "build": True,
        "seed_dir": "services/rudi-chat/seed",
        "seed_bucket_from": "meetrudi-base",
    },
}


def _exe(name):
    return shutil.which(name) or name


def _run(cmd):
    print(">", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        sys.exit(result.returncode)


def _capture(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def _stack_output(stack, key):
    cmd = [
        _exe("aws"), "cloudformation", "describe-stacks",
        "--stack-name", stack,
        "--region", REGION,
        "--profile", PROFILE,
        "--query", "Stacks[0].Outputs[?OutputKey=='%s'].OutputValue" % key,
        "--output", "text",
    ]
    return (_capture(cmd).stdout or "").strip()


def _build(comp):
    build_dir = os.path.join(".aws-sam", comp["stack"])
    _run([_exe("sam"), "build", "--template", comp["template"], "--build-dir", build_dir])
    return os.path.join(build_dir, "template.yaml")


def _deploy(comp, template_file):
    cmd = [
        _exe("sam"), "deploy",
        "--template-file", template_file,
        "--stack-name", comp["stack"],
        "--region", REGION,
        "--profile", PROFILE,
        "--capabilities", *CAPABILITIES,
        "--resolve-s3",
        "--no-confirm-changeset",
        "--no-fail-on-empty-changeset",
        "--tags", "project=meetrudi", "component=%s" % comp["stack"],
    ]
    _run(cmd)


def _seed(comp):
    if "seed_dir" not in comp:
        return None
    bucket = _stack_output(comp["seed_bucket_from"], "DataBucketName")
    if not bucket:
        print("!! could not resolve data bucket from %s; is it deployed? Skipping seed."
              % comp["seed_bucket_from"])
        return None
    print("Seeding %s -> s3://%s/" % (comp["seed_dir"], bucket))
    _run([
        _exe("aws"), "s3", "cp", comp["seed_dir"], "s3://%s/" % bucket,
        "--recursive", "--region", REGION, "--profile", PROFILE,
    ])
    return bucket


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in COMPONENTS:
        print("Usage: python deploy.py <component>")
        print("Components: %s" % ", ".join(COMPONENTS))
        sys.exit(2)

    name = sys.argv[1]
    comp = COMPONENTS[name]
    print("=== Deploying '%s' (stack %s, region %s, profile %s) ===\n"
          % (name, comp["stack"], REGION, PROFILE))

    template_file = _build(comp) if comp.get("build", True) else comp["template"]
    _deploy(comp, template_file)
    bucket = _seed(comp)

    print("\n=== DONE: %s ===" % comp["stack"])
    if name == "base":
        print("Data bucket :", _stack_output("meetrudi-base", "DataBucketName"))
        print("Runner role :", _stack_output("meetrudi-base", "LambdaRunnerRoleArn"))
    elif name == "ask-ai":
        b = bucket or _stack_output("meetrudi-base", "DataBucketName")
        print("Function URL:", _stack_output("meetrudi-ask-ai", "FunctionUrl"))
        print("Data bucket :", b)
        print("Prompt file :", "s3://%s/prompts/howcanihelp_prompt.md" % b)
        print("Context file:", "s3://%s/contexts/rudi-context.md" % b)
    elif name == "rudi-chat":
        print("Function URL:", _stack_output("meetrudi-rudi-chat", "FunctionUrl"))
        print("Data bucket :", bucket or _stack_output("meetrudi-base", "DataBucketName"))
        print(">> Put this Function URL into site/try-rudi.html (RUDI_CHAT_URL), then push.")


if __name__ == "__main__":
    main()
