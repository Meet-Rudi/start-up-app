# User to-do — actions only you can take

> Things Claude cannot do with the `rudi-deployer` profile (console-only IAM, secrets, quotas,
> vendor accounts). None blocks work today. Each item says **what it blocks**, so it gets done
> when it starts standing in the way. Claude adds new items here instead of stopping work, and
> ticks them only after you confirm. Last updated: 2026-10-28.

## Open

### 1. Raise the Lambda concurrency quota (10 → 1,000)

- **Blocks:** any load beyond ~10 simultaneous Lambda runs across the whole account (all
  services share the 10), the first platform stacks in prod, arena batches with more parallelism.
- **How:** Console → Service Quotas → AWS services → AWS Lambda → *Concurrent executions* →
  Request increase → `1000` (region eu-central-1). Or, if the deployer is allowed to:

  ```cmd
  aws service-quotas request-service-quota-increase ^
    --service-code lambda ^
    --quota-code L-B99A9384 ^
    --desired-value 1000 ^
    --region eu-central-1 ^
    --profile rudi-deployer
  ```

  AWS may take a few days to approve it.

### 2. Create the IAM permissions boundary for platform roles

- **Blocks:** the first platform stack that declares its own IAM roles (`meetrudi-internal-*` /
  `meetrudi-tenant-*`). Until then, platform templates reuse console-made roles.
- **How:** follow [infra/iam/README.md](infra/iam/README.md) §0. In short:
  1. Create the managed policy **`meetrudi-boundary`** from
     [`infra/iam/meetrudi-boundary.json`](infra/iam/meetrudi-boundary.json).
  2. Create the managed policy **`meetrudi-deployer-platform-roles`** from
     [`infra/iam/rudi-deployer.boundary-roles-add.json`](infra/iam/rudi-deployer.boundary-roles-add.json)
     and attach it to the user `rudi-deployer`.
- Tell Claude when done; CLAUDE.md §7 then allows template-made platform roles.

### 3. PlanetScale: dev database, DPA, credentials secret

- **Blocks:** running the platform schema migrations (`db/migrations/`) against a real database,
  and every platform component that reads or writes the registry. Writing and unit-testing the
  schema does not need it.
- **How:**
  1. Sign PlanetScale's DPA (with the 2021 SCCs) for the organisation (decision #30).
  2. Create a **Postgres** database in **AWS eu-central-1 (Frankfurt)**, e.g.
     `meetrudi-internal-dev`, and a separate role with a password for the platform. That role
     must **not** be the default admin/superuser and must **not** have `BYPASSRLS`, otherwise
     tenant isolation is skipped (db/README.md). Claude can give you the exact `CREATE ROLE`
     statement when you get here.
  3. Store its connection details in Secrets Manager (the deployer cannot create secrets):
     Console → Secrets Manager → Store a new secret → *Other type of secret* → Plaintext →
     `{"host": "...", "port": 5432, "database": "...", "user": "...", "password": "...", "sslmode": "verify-full"}`
     → name it exactly **`meetrudi/internal/dev/planetscale`** (region eu-central-1).

## Later (not needed before Phase 2)

- **Meta Tech Provider enrolment** (via Twilio's ISV programme) — blocks onboarding a tenant's
  own WhatsApp account.

## Done

_(nothing yet)_
