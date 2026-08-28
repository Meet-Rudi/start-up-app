# IAM — created manually in the AWS Console

The `rudi-deployer` user is **not** allowed to create IAM objects, so all roles/policies here
are created by hand in the console. CloudFormation/SAM only *references* them by ARN.

Account: `949753869755` · Region: `eu-central-1`

## 1. Role: `meetrudi-lambda-runner`
The shared execution role for meetrudi Lambdas.

Console → IAM → Roles → **Create role** → **Custom trust policy** → paste
[`meetrudi-lambda-runner.trust.json`](meetrudi-lambda-runner.trust.json) →
add permissions: **Create inline policy** (JSON) → paste
[`meetrudi-lambda-runner.permissions.json`](meetrudi-lambda-runner.permissions.json) →
name the role exactly **`meetrudi-lambda-runner`**.

Grants: CloudWatch Logs, read/write on `meetrudi-ai-data-949753869755`, and
`secretsmanager:GetSecretValue` on `meetrudi*` secrets.

## 2. Allow the deployer to pass the role
Console → IAM → Users → `rudi-deployer` → its policy `RudiDeployPolicy` (Edit) → add the
statement in [`rudi-deployer.passrole-add.json`](rudi-deployer.passrole-add.json)
(or attach it as a new inline policy). Required so SAM can attach the role to the Lambda.

## 3. Secret: `meetrudi/test-console/auth` (personality test console login)
The internal test console (`meetrudi-test-console` Lambda) gates on a single fixed
email+password held in Secrets Manager. **No IAM change needed** — the runner role's
`ExternalApiSecrets` statement already allows `GetSecretValue` on `meetrudi*`.

Console → Secrets Manager → **Store a new secret** → *Other type of secret* → **Plaintext** →
paste JSON matching [`meetrudi-test-console-auth.example.json`](meetrudi-test-console-auth.example.json)
with your own values → name it exactly **`meetrudi/test-console/auth`** (region `eu-central-1`).

- `email` + `password`: the login the tester types.
- `token`: a long random string the API returns on successful login and then requires on every
  call (the SPA stores it in `sessionStorage`). Generate e.g. `python -c "import secrets;print(secrets.token_hex(24))"`.

The Lambda **fails closed** (401 on every route) until this secret exists.

### Login lockout & admin unlock
After **10 consecutive failed logins** the email is locked (403 on every attempt, even with the
correct password) until an admin clears it. The lock state is a single S3 object:

    s3://meetrudi-ai-data-949753869755/test-console/login-state.json

To **unlock**: delete that object, or edit it and set the email's `"locked_at": ""` and
`"failed": 0`. e.g.:
```cmd
aws s3 rm s3://meetrudi-ai-data-949753869755/test-console/login-state.json ^
  --region eu-central-1 --profile rudi-deployer
```
A successful login also resets the counter; only the configured email is ever tracked.

## When the account/region/bucket changes
These JSONs hard-code the account id, region, and bucket name (IAM has no CloudFormation
substitution). Update them if any of those change.

## 4. Secret: `meetrudi/tts/token` (Piper TTS Function URL guard)

`meetrudi-tts` exposes a public Function URL, so every synthesis request must carry a shared
token. **No IAM change needed** — the runner role's `ExternalApiSecrets` statement already
allows `GetSecretValue` on `meetrudi*`. But `rudi-deployer` is **not** permitted to *create*
secrets, so this is a console step.

Console → Secrets Manager → **Store a new secret** → *Other type of secret* → **Plaintext** →
paste JSON matching [`meetrudi-tts-token.example.json`](meetrudi-tts-token.example.json) with
your own value → name it exactly **`meetrudi/tts/token`** (region `eu-central-1`).

Generate the token with:

```cmd
python -c "import secrets;print(secrets.token_hex(24))"
```

The function **fails closed**: `503` on every synthesis request until this secret exists. The
unauthenticated `{"ping": true}` health check keeps working either way and reports
`auth_configured: false`, which is the quickest way to confirm the secret landed.

The same value goes into `meetrudi-voice-bench` only indirectly — that function reads the
secret itself via `PIPER_TTS_SECRET`, so the token is never copied into an environment variable.

## 5. Let the deployer read the call dispatch token

Needed by the voice test scripts (`services/call/rudi_test_call_*.py`), which place a real call
from the workstation. Without it they fail with `AccessDeniedException` on
`secretsmanager:GetSecretValue`.

Console → IAM → Users → `rudi-deployer` → policy `RudiDeployPolicy` (**Edit** → JSON) → add the
statement from [`rudi-deployer.dispatch-token-add.json`](rudi-deployer.dispatch-token-add.json)
to the `Statement` array → **Next** → **Save changes**.

Deliberately scoped to that one secret: `rudi-deployer` still cannot read the Twilio credentials
or any other secret, and read-only means it still cannot create or edit secrets (console-only,
per CLAUDE.md §7).

**Escape hatch if you can't touch IAM right now:** the scripts also accept the token from
`RUDI_DISPATCH_TOKEN` (or `--token`), copied out of the Secrets Manager console. That works, but
it puts the token in your environment — the IAM statement exists so it never has to be.

## 6. Tester console (third-party cohort)

Three pieces, all console-only. The tester console **fails closed** without them: the admin pane
answers `503 admin_not_configured` and, without the SES statement, registration still succeeds
but no verification mail goes out (the admin pane can re-issue links once mail works).

### 6a. Extend the runner role
Console → IAM → Roles → `meetrudi-lambda-runner` → its inline policy (**Edit** → JSON) → add
both statements from
[`meetrudi-lambda-runner.tester-console-add.json`](meetrudi-lambda-runner.tester-console-add.json)
to the `Statement` array → **Save changes**.

- `TesterConsoleMail` — SES send, pinned by condition to the one verified sender address, so a
  compromised function cannot send as anyone else. Edit the address to match what you verify in
  SES below.
- `TesterConsoleDeleteObjects` — `s3:DeleteObject` on **only** `tester-console/sessions/*` and
  `tester-console/tokens/*`. Burning a single-use link has to actually remove it (a replay must
  find nothing), and killing a session has to remove it too. Tester profiles, feedback and
  conversations are deliberately **not** deletable by this role — erasure is its own reviewed
  path in phase 2.

### 6b. Secret: `meetrudi/tester-console/admin`
Console → Secrets Manager → **Store a new secret** → *Other type of secret* → **Plaintext** →
paste JSON matching
[`meetrudi-tester-console-admin.example.json`](meetrudi-tester-console-admin.example.json) →
name it exactly **`meetrudi/tester-console/admin`** (region `eu-central-1`).

No IAM change needed — the runner role already reads `meetrudi*` secrets.

### 6c. Verify the SES sender
Console → Amazon SES (**eu-central-1**) → Identities → **Create identity** → verify either the
domain `meetrudi.eu` or the single address you intend to send from. A domain identity is better:
it survives changing the local part and lets you set DKIM.

New SES accounts start in the **sandbox**, where you may only send to *verified* addresses. For
a closed cohort of invitees that is survivable but awkward — request production access before
the cohort starts, or verify each tester's address by hand first.

Then redeploy with the sender wired in (see the deploy command in
[`docs/tester-console.md`](../../docs/tester-console.md)).
