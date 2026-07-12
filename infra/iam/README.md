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
