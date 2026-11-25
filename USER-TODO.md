# User to-do — actions only you can take

> Things Claude cannot do with the `rudi-deployer` profile (console-only IAM, secrets, quotas,
> vendor accounts). None blocks work today. Each item says **what it blocks**, so it gets done
> when it starts standing in the way. Claude adds new items here instead of stopping work, and
> ticks them only after you confirm. Last updated: 2026-11-24.

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

### 3. PlanetScale: confirm the DPA is signed

- **Blocks:** storing any tenant's data in `meet-rudi-backbone` (Prime Directive §0.1/§0.3).
- **How:** check that PlanetScale's DPA (with the 2021 SCCs) is signed for the organisation that
  owns `meet-rudi-backbone` (decision #30). Another project (OrgAI) also uses PlanetScale: make
  sure it is MEET_RUDI's organisation that is covered.

### 4. Groq: move off the free tier

- **Blocks:** call turns staying fast. The free tier gives 8,000 tokens a minute per model; a call
  turn asks for about 2,500 (4,300 in a first call), so after about three turns a call falls to
  slower providers (2–7 s pauses), and WhatsApp shares the same allowance.
- **How:** Groq console → the organisation that owns the key in `meetrudi-groq-firstkey` →
  Billing → upgrade to the Developer (pay-as-you-go) tier. The arena's price note for
  gpt-oss-120b is USD 0.15 in / 0.60 out per million tokens: about a cent per call. Check that
  Groq's terms and DPA still fit (Prime Directive §0.1, pseudonymized payloads only).

### 5. Let the request form remove a draft once it is sent (optional)

- **Blocks:** nothing. Without it, a sent adoption request leaves its partial file behind in
  `platform/prospects/partial/`, marked `"state": "complete"` (the follow-up rule skips it).
- **How:** [infra/iam/README.md](infra/iam/README.md) §9: add the one statement in
  [`meetrudi-lambda-runner.prospects-add.json`](infra/iam/meetrudi-lambda-runner.prospects-add.json)
  to the `meetrudi-lambda-runner` inline policy (delete only below
  `platform/prospects/partial/`).

## Later (not needed before Phase 2)

- **Meta Tech Provider enrolment** (via Twilio's ISV programme) — blocks onboarding a tenant's
  own WhatsApp account.
- **PlanetScale secret names (decision #42)** — blocks the first template-made platform role
  (after #2) that reads the database. `meetrudi-boundary` lets platform roles read only
  `meetrudi/internal/*` and `meetrudi/tenant/*`, so they cannot read
  `meetrudi/planetscale-backbone/…` (today's runner uses the legacy role, which can). When it
  comes up: store copies as `meetrudi/internal/prod/planetscale/primary-write`,
  `…/primary-read` and `…/replica-read`, or allow the existing path in the boundary.
- **PlanetScale Admin password for the runner** — blocks only `python db/migrate.py apply`
  through the runner (migrations go in through the console today). Put an Admin-role password
  into `meetrudi/planetscale-backbone/primary-main-write` (today a Writer password).
- **Qdrant Cloud (decision #39)** — only once a skill needs vector memory. Free cluster in an EU
  region (AWS eu-central-1 if offered), Qdrant's DPA signed, API key stored in Secrets Manager
  as `meetrudi/internal/{stage}/qdrant`.

## Done

- **PlanetScale schema (was #3), 2026-11-15.** Migrations 0001–0004 applied in the console on
  `meet-rudi-backbone` (Vitess, eu-central-1), each with its `meta_migrations` record. Claude's
  `plan` through the runner: 37 tables, 4 applied, nothing pending or unfinished, no drift.
  Future migrations: `python db/console_sql.py <n>`, paste from `db/console/`.
