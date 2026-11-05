# CLAUDE.md — Coding Rules & Conventions for MEET_RUDI

> Machine-loaded each session. The *why/what* lives in [project.md](project.md); this file is
> the *how*. When product context is needed, read project.md. Last updated: 2026-11-04.
>
> **Platform pivot (2026-10-28):** Rudi is becoming a multi-tenant conversational platform. Its
> design is [docs/platform/blueprint.md](docs/platform/blueprint.md); its binding rules (ids,
> resource names, run time, memory, coexistence) are
> [docs/platform/conventions.md](docs/platform/conventions.md). Read both before writing
> platform code.

---

## 0. Prime Directives (never violate)

1. **No PII leaves the AWS EU plane.** The neo-cloud GPU plane and any 3rd-party AI API receive
   only **minimized, pseudonymized** payloads (opaque IDs, never names/contact/free PII).
   *Approved exception (decision #30):* PlanetScale Postgres in eu-central-1 is the platform's
   system of record, under its DPA and the 2021 SCCs. No other datastore outside AWS without a
   new recorded decision.
2. **Not a medical device / no medical advice.** Never produce diagnostic or treatment output.
   Route medical-advice requests and red-flag content through the guardrail layer (§6).
3. **EU residency only.** Every datastore, queue, bucket, model endpoint, and 3rd-party service
   must be EU-region and covered by a DPA. If unsure, ask before wiring it in.
4. **Everything swappable is behind an interface** — WhatsApp provider, model gateway, storage.
   No vendor SDK calls in business logic.
5. **Secrets never in code or git.** Use AWS Secrets Manager / SSM Parameter Store; reference by
   name. No tokens, keys, or numbers in source, fixtures, or logs.

---

## 0a. Naming convention (mandatory)

All AWS resources we create — **Lambdas, queues, IAM roles, and related infra** — use the
**`meetrudi-`** prefix (e.g. `meetrudi-ask-ai`, `meetrudi-lambda-runner`). SSM/Secrets keys use
the `meetrudi/` path prefix. S3 data bucket: `meetrudi-ai-data-<account-id>`.

**Platform resources** (everything new from 2026-10-28) add a plane, a stage and, for tenant
traffic, a cell — build names with `rudi_platform.naming`, never by hand:

- `meetrudi-internal-{stage}-{component}` — platform-managing (console API, registry, arena,
  skill factory, migrations).
- `meetrudi-tenant-{stage}-{cell}-{component}` — serving tenant traffic (adapters, router,
  engine, skills, scheduler); skill Lambdas are `skill-<slug>`.
- Secrets `meetrudi/{plane}/{stage}/…`; tenant KMS alias `alias/meetrudi-tenant-{stage}-{ten_…}`.
- Existing unprefixed resources are legacy and keep their names until their service is cut over.

## 0b. Identifiers (mandatory)

Every stored object id is **`{cat}_` + 32 lowercase Crockford base32 characters**, minted with
`rudi_platform.ids.new_id(Cat.X)` (end-user ids: `pseudonym_id(...)`). The three-letter prefixes
are registered in `Cat` and in the table in
[docs/platform/conventions.md](docs/platform/conventions.md) §1 — add a category to both in the
same change, never rename or reuse one, and never hand-build an id string.

## 1. Languages, Repo & Layout

- **Python** = AI / messaging / orchestration core. **TypeScript/JS** = front-end & ops UI.
- **Mono-repo.** Proposed top-level layout (create dirs as features land — don't scaffold empty):
  ```
  /services        # deployable units (Lambda fns, Fargate services)
  /packages        # shared Python libs (domain, memory, gateway, guardrails, wa-provider)
  /web             # TypeScript/JS front-end & ops UI
  /infra           # IaC: /infra/sam, /infra/cfn (Fargate+net), /infra/neocloud
  /corpus          # curated guideline corpus + ingestion (RAG source)
  /evals           # offline eval suites & datasets
  /db              # platform Postgres (PlanetScale) migrations — see db/README.md
  /docs            # project.md lives at root; deeper docs here
  ```
- **Platform core:** `packages/rudi_platform` — ids, naming, skill contract, manifest and
  composer, run budget and hand-over, model gateway, de-identification, leak guard, timeline,
  OCEAN persona, i18n catalogue, access (roles as data), registry store and publishing,
  journey, conversation memory (incl. CM's legacy S3 adapter), the Turn Engine and the arena
  door. Real-time skills: `packages/rudi_skills` (one package per skill). Tenants in code:
  `packages/rudi_tenants` (`cm.py`). Standard library only. Tests (all three packages):
  `python -m unittest discover -s packages/rudi_platform/tests -v`.
- **CM parity:** `tests/test_engine_parity.py` (WhatsApp) and `tests/test_voice_parity.py`
  (calls) hold CM's clone to the live engines turn by turn. A change to a CM prompt or rule goes
  into both `services/whatsapp` or `services/call` and the skill package, or the tests fail.
- **Platform model calls go through `rudi_platform.gateway.ModelGateway` only**, with a cascade
  from `resolve_cascade(endpoints, tenant_policy, tier)`; never call a provider directly.
- **Python:** 3.12+, type hints required, `ruff` (lint+format), `mypy` for typed packages,
  `pytest`. Prefer `pydantic` models at boundaries — but not in Lambda code paths yet (zip
  bundles are built on Windows; see conventions §7). New platform code is ruff-clean.
- **TypeScript:** strict mode on, ESLint + Prettier, no `any` without a written reason.

---

## 2. Core Abstractions (mandatory seams)

These are the swap-points the whole architecture depends on. Code against the interface only.

- **`WhatsAppProvider`** — send/receive text/image/audio, template send, window-state queries.
  Implementations: `TwilioProvider` (PoC) → `MetaCloudProvider` (later). Business logic never
  imports a vendor SDK directly.
- **`ModelGateway`** — `embed()`, `generate()`, (later `transcribe()`, `vision()`). Routes per
  workload to **API** or **self-hosted** backends. Callers never know which backend served them.
- **`MemoryStore`** — profile read/write, event append, summary get/set, vector upsert/query.
  **Pluggable per use case** (conventions §5): the legacy S3 store (serves CM until replaced),
  S3 fast-memory snapshots for latency-critical real-time skills, and Postgres (PlanetScale)
  as the record. The interface must not leak any backend's specifics.
- **Platform seams** (blueprint §3): `ChannelAdapter` (generalises `WhatsAppProvider`),
  `RegistryStore`, `ScheduleStore`, `IdentityProvider`, and the skill envelope
  `SkillRequest`/`SkillResult` that every skill — real-time, turn or background — speaks.

When adding a feature, ask: *does this belong behind one of these interfaces?* If yes, extend
the interface — don't reach around it.

---

## 3. Conversation Engine Rules

- **Window state is explicit.** Every outbound decision checks: is the user **in-window**
  (<24h since last inbound) or **out-of-window**?
  - In-window → free-form, full-persona reply allowed.
  - Out-of-window → **pre-approved template messages only** (proactive check-ins use the
    lightweight "tap on the shoulder" template; rich convo resumes after the user replies).
- **Per-number quality is sacred.** Never design flows that risk blocks/reports or spread users
  across numbers to dodge limits. One user → one fixed Rudi/number.
- **Proactive cadence ~2–3×/week**, driven by scheduled/event triggers, always via templates
  when out-of-window.
- **i18n-ready always.** No hard-coded user-facing strings. Locale lives on the user profile;
  prompts and WhatsApp templates are keyed by language. English content first.

---

## 4. AI / Orchestration Rules

- **Own orchestration**, framework-light. Keep the per-message pipeline explicit and readable:
  ingest → (ASR/vision) → context assembly → reasoning → **guardrail gate** → send.
- **Memory injection:** assemble context from **structured profile + cached recent summary +
  vector recall** — in that priority. Keep prompt payloads minimal (cost + PII discipline).
- **Cost-aware by default:** right-size models, cache aggressively, batch where latency allows
  (we have 10–15s budget). Tag inference calls so per-user cost is observable
  (target ≤ €13/active-user/mo).
- **Phased models:** reasoning on API first; embeddings self-hosted next; reasoning self-hosted
  only when utilization justifies it — all via `ModelGateway`, no caller changes.
- **Source of truth = Postgres** (profile + event log). Vectors/summaries are derived and
  rebuildable; never the canonical record.

---

## 5. Data, Privacy & PII Rules

- **Pseudonymize before the GPU plane / any external API.** Build and use a single
  de-identification helper; do not hand-roll per call.
- **Right to erasure** must cascade across **all** stores — profile, event log, vectors, caches,
  and any derived summary. Design deletes to be complete, not best-effort.
- **Consent gates processing.** Check consent state before health-related handling.
- **No PII in logs.** Log opaque IDs and event types, never message content or contact data.
  Structured logging only.

---

## 6. Guardrail Rules (in the live send path)

Before any outbound generated message is sent:
1. **RAG-ground** against the curated corpus where the turn is guidance-adjacent.
2. **Run risk classifiers** (hypoglycemia, disordered-eating, red-flag symptoms, explicit
   medical-advice requests). On trigger → **deflect + escalate**, suppress advice-like content,
   point the user back to their clinician.
3. **Knowledge graph** only when a case genuinely needs structured reasoning — not a default.
4. Treat the guardrail gate as **non-bypassable**: a generation that fails the gate is not sent.
- Knowledge that informs guardrails (corpus, classifier thresholds) is **versioned**; changes
  go through evals (§8).

---

## 7. Infrastructure & Deployment

- **IaC split:** **AWS SAM** for the serverless app plane (Lambda + API Gateway + events);
  **plain CloudFormation (or a thin Terraform module)** for Fargate + networking; **neo-cloud
  provisioned via its own IaC/API**. Don't force SAM to do Fargate/GPU.
- **Lambda = container images.** Keep functions single-purpose and fast.
- **13-minute rule (mandatory):** no Lambda run does work past 13 minutes. Long work uses
  `rudi_platform.runtime.budget.RunBudget` and hands over via `runtime.handover.hand_over`
  (checkpoint first, then an async invoke of itself) starting at 12 minutes. Keep `Timeout: 900`
  on such functions as AWS's backstop only.
- **Fargate** for long-running orchestrator/workers/serving shims.
- **Async-first:** prefer SQS/event decoupling over synchronous chains; the conversation can
  tolerate 10–15s.
- Every resource: **EU region, tagged** (env, service, cost-center), least-privilege IAM.

### IAM (mandatory)
- **Legacy and everything until the boundary exists — console-only.** The `rudi-deployer` user
  **cannot create IAM roles/policies.** **Never** put `AWS::IAM::*` resources in SAM/CFN
  templates. Instead, hand the user **role/policy JSON files** (store them in `infra/iam/`) to
  create in the AWS Console, and have templates **reference** the role by ARN
  (`!Sub "arn:aws:iam::${AWS::AccountId}:role/meetrudi-..."`).
- Lambdas that use a pre-created role require the deployer to have `iam:PassRole` on it — supply
  that as a console policy addition too.
- **Platform roles (decision #35), only after `infra/iam/README.md` §0 is done** (the user
  confirms `meetrudi-boundary` and `meetrudi-deployer-platform-roles` exist): templates may
  declare `AWS::IAM::Role` resources named `meetrudi-internal-*` / `meetrudi-tenant-*`, each with
  `PermissionsBoundary: !Sub "arn:aws:iam::${AWS::AccountId}:policy/meetrudi-boundary"`. Managed
  policies, users and the boundary itself stay console-only.

### Deployment workflow (mandatory)
- **One-command deploys.** A Python wrapper (`deploy.py`) prepares and ships a component: the
  user runs **`python deploy.py <component-name>`** and nothing else. The script runs
  `sam build` + `sam deploy` with **all** flags pre-filled (stack name, `--region eu-central-1`,
  `--profile rudi-deployer`, `--capabilities CAPABILITY_IAM`, `--resolve-s3`,
  `--no-confirm-changeset`). Never hand the user a manual multi-step deploy sequence to run.
- **Always give complete commands.** Every command provided must include **all** parameters,
  file/object names, and config values. Never leave the user to assemble, guess, or fill in
  arguments themselves.
- **CMD line continuation = `^`.** The user runs commands in Windows `cmd.exe`. Multi-line
  commands must use `^` (not `\`); avoid PowerShell-only syntax unless explicitly asked.
- **On any change needing redeploy** (Lambda/Fargate code or infra altered), output the
  **full, copy-paste-ready deployment command sequence** at the end of the work.

---

## 8. Testing & Evals

- **Unit tests** for domain logic; **contract tests** for each interface impl
  (`WhatsAppProvider`, `ModelGateway`, `MemoryStore`) so backends stay swappable.
- **Offline evals** (`/evals`) for prompt/guardrail changes — never ship a guardrail or prompt
  change without running the relevant eval suite.
- Mock external services in tests; **no real PII** in fixtures (synthetic data only).
- Conversation-engine tests must cover **in-window vs out-of-window** behavior explicitly.

---

## 9. Git & Workflow

- Work on a **branch**; do not commit straight to `main`. Commit/push only when the user asks.
- Conventional, scoped commit messages (e.g. `feat(memory): ...`, `fix(wa-provider): ...`).
- Keep PRs small and seam-aligned (one interface/feature at a time).
- Never commit secrets, `.env`, real numbers, or PII. Maintain `.gitignore` accordingly.

---

## 10. Working Agreement with Claude

- **Do not write application code until explicitly asked.** Blueprint/rules/docs are fine;
  features are not, until greenlit.
- When a task touches a Prime Directive (§0), call it out before proceeding.
- **Coexistence (mandatory):** while the platform is built, today's CM service stays fully
  working at every step. Replace a component only after its successor is live, tested and holds
  the data; never leave an existing Rudi skill unavailable because its successor is under
  construction (conventions §6).
- Prefer extending an existing abstraction over adding a new vendor dependency; if a new
  dependency or service is needed, flag EU-residency + DPA implications first.
- When product intent is unclear, ask — don't assume. Keep the Decisions Log in project.md
  current as choices are made.
- **Hands-off deploys & complete commands:** honor the Deployment workflow rules in §7 —
  one-command `python deploy.py <name>` deploys, fully-parameterized commands, `^` for CMD
  line breaks, and a full deployment command sequence printed whenever a redeploy is needed.
