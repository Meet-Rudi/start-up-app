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

## When the account/region/bucket changes
These JSONs hard-code the account id, region, and bucket name (IAM has no CloudFormation
substitution). Update them if any of those change.
