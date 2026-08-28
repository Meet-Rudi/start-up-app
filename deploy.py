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
import json
import shutil
import subprocess

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
ACCOUNT_ID = "949753869755"   # MEET_RUDI account; a deploy aborts if PROFILE resolves anywhere else
CAPABILITIES = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"]
ROOT = os.path.dirname(os.path.abspath(__file__))

# Keep ALL SAM state inside the repo. By default SAM writes its global config (installation_id /
# telemetry metadata.json) to %APPDATA%\AWS SAM — a protected Roaming dir that is permission-denied
# in some shells and crashes the build. __SAM_CLI_APP_DIR relocates it here (under the already
# .gitignored .aws-sam/). Telemetry off so no metric write is attempted at all.
os.environ["__SAM_CLI_APP_DIR"] = os.path.join(ROOT, ".aws-sam", "samhome")
os.environ.setdefault("SAM_CLI_TELEMETRY", "0")
os.makedirs(os.environ["__SAM_CLI_APP_DIR"], exist_ok=True)

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
    "whatsapp": {
        "stack": "meetrudi-whatsapp",
        "template": "services/whatsapp/template.yaml",
        "build": True,
        "seed_dir": "services/whatsapp/seed",   # WhatsApp-aware prompt(s)
        "seed_bucket_from": "meetrudi-base",
        # Tester console settings. The dispatch URL is read straight off the call stack so the
        # two are never wired together by hand; the rest come from deploy.local.json because
        # they are phone numbers and addresses, which do not belong in git.
        "params": {
            "CallDispatchUrl": {"from_stack": ("meetrudi-call", "DispatchUrl")},
            "TesterConsoleOrigin": {"from_config": "tester_console_origin"},
            "TesterConsoleBase": {"from_config": "tester_console_base"},
            "TesterMailFrom": {"from_config": "tester_mail_from"},
            "TesterMailFromName": {"from_config": "tester_mail_from_name"},
            "TesterSupportEmail": {"from_config": "tester_support_email"},
            "TesterWaNumber": {"from_config": "tester_wa_number"},
            "TesterWaJoinPhrase": {"from_config": "tester_wa_join_phrase"},
        },
    },
    "registration": {
        "stack": "meetrudi-registration",
        "template": "services/registration/template.yaml",
        "build": True,   # consent intake endpoint (CM pilot form)
    },
    "tts": {
        "stack": "meetrudi-tts",
        "template": "services/tts/template.yaml",
        "build": True,
        # Piper needs native wheels for Lambda's platform, so the layer is assembled with
        # pip --platform before SAM packages it. Without this the layer would carry Windows
        # binaries and the function would fail at import.
        "prebuild": "services/tts/build_layer.py",
    },
    "call": {
        "stack": "meetrudi-call",
        "template": "services/call/template.yaml",
        "build": True,   # Plan A: Twilio ConversationRelay + API GW WebSocket, no Fargate
    },
    "voice-bench": {
        "stack": "meetrudi-voice-bench",
        "template": "services/voice-bench/template.yaml",
        "build": True,   # Phase-0 spoken call bench (ASR + learn/goal/commit + TTS)
        # No seed_dir: the bench deliberately reads the SAME prompt/context assets that
        # rudi-chat and whatsapp already seed, so there is one source of truth for Rudi's words.
    },
}


# Deployment settings that must NOT live in source: phone numbers, sender addresses, the join
# phrase (CLAUDE.md §0.5 — no numbers in source). They live in a git-ignored file at the repo
# root, and every one of them is optional: a missing value falls back to the template default,
# which for the tester console means "not configured yet" and a component that fails closed
# rather than half-working.
LOCAL_CONFIG = os.path.join(ROOT, "deploy.local.json")


def _local_config():
    if not os.path.exists(LOCAL_CONFIG):
        return {}
    try:
        with open(LOCAL_CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError) as e:
        print("!! %s is unreadable (%s) — deploying with template defaults."
              % (os.path.basename(LOCAL_CONFIG), e))
        return {}


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


def _preflight():
    """Refuse to deploy unless PROFILE really resolves to the MEET_RUDI account.

    This workstation also holds credentials for other projects (the `default` profile points at a
    different account). Every AWS/SAM call below already pins `--profile`, which ignores AWS_PROFILE
    and ambient credentials — this is the belt-and-braces check: if the profile is missing, expired,
    or ever repointed, we stop before a single resource is created in the wrong account."""
    r = _capture([
        _exe("aws"), "sts", "get-caller-identity",
        "--profile", PROFILE, "--region", REGION,
        "--query", "Account", "--output", "text",
    ])
    account = (r.stdout or "").strip()
    if not account:
        print("!! cannot resolve an AWS identity for profile %r — nothing deployed." % PROFILE)
        detail = (r.stderr or "").strip()
        if detail:
            print("   %s" % detail)
        print("   Configure it with:  aws configure --profile %s" % PROFILE)
        sys.exit(2)
    if account != ACCOUNT_ID:
        print("!! ABORT: profile %r resolves to account %s, but MEET_RUDI is %s."
              % (PROFILE, account, ACCOUNT_ID))
        print("   Nothing was deployed. Fix the profile before retrying.")
        sys.exit(2)
    print("Account check OK: %s (profile %s)\n" % (account, PROFILE))


def _build(comp):
    build_dir = os.path.join(".aws-sam", comp["stack"])
    _run([_exe("sam"), "build", "--template", comp["template"], "--build-dir", build_dir])
    return os.path.join(build_dir, "template.yaml")


def _parameter_overrides(comp):
    """Resolve this component's CloudFormation parameters.

    Two sources, neither of which puts a phone number or an address in git:
      - `from_stack`: read another stack's output (e.g. the call dispatcher's Function URL), so
        the operator never copies a URL between stacks by hand.
      - `from_config`: read deploy.local.json.
    A value that resolves to nothing is omitted, leaving the template's own default in place.
    """
    spec = comp.get("params") or {}
    if not spec:
        return []
    cfg = _local_config()
    overrides = []
    for key, source in sorted(spec.items()):
        if "from_stack" in source:
            stack, output = source["from_stack"]
            value = _stack_output(stack, output)
            if value in ("", "None"):
                print("   (%s: %s has no %s output yet — using the template default)"
                      % (key, stack, output))
                continue
        else:
            value = str(cfg.get(source["from_config"], "")).strip()
            if not value:
                print("   (%s: not set in deploy.local.json — using the template default)" % key)
                continue
        # `sam` is a .CMD on Windows, so the override string is re-parsed by cmd.exe: `<` and `>`
        # are redirection operators and abort the deploy with "The system cannot find the file
        # specified", while `"` breaks SAM's own parser. Refuse loudly rather than ship a
        # half-applied parameter — a sender that silently became "Rudi" is exactly this bug.
        bad = [c for c in '"<>|&^' if c in value]
        if bad:
            print("!! %s contains %s, which cannot be passed through sam deploy — skipping it."
                  % (key, " ".join(repr(c) for c in bad)))
            print("   Use a value without those characters (a display name and an address are")
            print("   separate settings for exactly this reason).")
            continue
        # SAM re-parses the override string itself and splits unquoted values on whitespace, so a
        # display-name sender like `Rudi test <support@meetrudi.eu>` would arrive as just "Rudi".
        # Quoting the value keeps it whole.
        overrides.append('%s="%s"' % (key, value) if any(c.isspace() for c in value)
                         else "%s=%s" % (key, value))
    return overrides


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
    overrides = _parameter_overrides(comp)
    if overrides:
        cmd += ["--parameter-overrides", *overrides]
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

    _preflight()

    if comp.get("prebuild"):
        print("Prebuild: %s\n" % comp["prebuild"])
        _run([sys.executable, comp["prebuild"]])

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
    elif name == "whatsapp":
        print("Webhook URL :", _stack_output("meetrudi-whatsapp", "WebhookUrl"))
        print("Console API :", _stack_output("meetrudi-whatsapp", "ConsoleApiUrl"))
        print("Test console:", _stack_output("meetrudi-whatsapp", "TestConsoleApiUrl"))
        print("Send test   :", _stack_output("meetrudi-whatsapp", "SendTestUrl"))
        print(">> Set the Webhook URL as the Twilio Sandbox 'When a message comes in' webhook (HTTP POST).")
        print(">> Console API + Send test need secret meetrudi/whatsapp/console-token (both fail CLOSED without it).")
        print(">> Outbound test: open  <Send test URL>?token=<TOKEN>&to=+32470...&body=Hello")
        print(">> Put the Console API URL into console/config.js (API_BASE) once auth is set.")
        print(">> Test console needs secret meetrudi/test-console/auth (fails CLOSED without it).")
        print(">> Put the Test console URL into site/test-console/test-config.js (API_BASE), then")
        print("   push to main -> GitHub Pages publishes https://meet-rudi.github.io/start-up-app/test-console/")
        print("")
        print("Tester API  :", _stack_output("meetrudi-whatsapp", "TesterApiUrl"))
        print(">> Put the Tester API URL into site/tester-console/config.js (API_BASE), then push.")
        print(">> Needs (once, console-only — see infra/iam/README.md §6):")
        print("     secret meetrudi/tester-console/admin   (admin pane 503s without it)")
        print("     the two statements in infra/iam/meetrudi-lambda-runner.tester-console-add.json")
        print("     a verified SES sender in eu-central-1 (no verification mails without it)")
        print(">> Settings come from deploy.local.json; copy deploy.local.example.json to start.")
    elif name == "tts":
        print("Function URL:", _stack_output("meetrudi-tts", "FunctionUrl"))
        print("Layer       :", _stack_output("meetrudi-tts", "LayerArn"))
        print(">> One-time: python services/tts/seed_voices.py   (uploads ~313MB of voices)")
        print(">> One-time: create secret meetrudi/tts/token (plaintext JSON {\"token\":\"...\"})")
        print("   Generate one with:  python -c \"import secrets;print(secrets.token_hex(24))\"")
        print("   The function returns 503 on every call until that secret exists.")
        print(">> Then redeploy voice-bench with TTS_PROVIDER=piper and the URL + token set.")
    elif name == "call":
        print("WebSocket   :", _stack_output("meetrudi-call", "WebSocketUrl"))
        print("Dispatch URL:", _stack_output("meetrudi-call", "DispatchUrl"))
        print(">> IAM (once): add infra/iam/meetrudi-lambda-runner.call-add.json to the")
        print("   meetrudi-lambda-runner inline policy. Rudi is SILENT on calls without it.")
        print(">> Secrets (once), console -> Secrets Manager, plaintext JSON:")
        print("     meetrudi/twilio/voice          {\"account_sid\":..,\"auth_token\":..,\"from_number\":\"+32...\"}")
        print("     meetrudi/call/dispatch-token   {\"token\":\"<random hex>\"}")
        print(">> Dry run (builds the record + TwiML, dials nobody):")
        print("     curl -X POST <Dispatch URL> -H \"Content-Type: application/json\" ^")
        print("       -d \"{\\\"token\\\":\\\"<TOKEN>\\\",\\\"dry_run\\\":true,\\\"config\\\":{...}}\"")
        print(">> Calls land in s3://<data-bucket>/calls/<call-id>/ .")
    elif name == "voice-bench":
        print("Function URL:", _stack_output("meetrudi-voice-bench", "FunctionUrl"))
        print("Data bucket :", _stack_output("meetrudi-base", "DataBucketName"))
        print(">> Paste the Function URL into site/voice-bench/voice-config.js (VOICE_BENCH_API),")
        print("   then push to main -> GitHub Pages publishes")
        print("   https://meet-rudi.github.io/start-up-app/voice-bench/")
        print(">> Reads the same prompts rudi-chat/whatsapp seed; deploy one of those first if")
        print("   prompts/rudi_guardrails.md is not in the bucket yet.")
        print(">> Calls land in s3://<data-bucket>/voice-bench/calls/<call-id>/ .")
    elif name == "registration":
        url = _stack_output("meetrudi-registration", "ConsentIntakeUrl")
        print("Consent intake URL:", url)
        print(">> Put this URL into web/cm-consent-form/*.html (API_BASE).")
        print(">> Records land in s3://<data-bucket>/registrations/consent_documents/ .")
        print(">> Enable S3 Versioning on the data bucket to keep an immutable consent audit trail.")


if __name__ == "__main__":
    main()
