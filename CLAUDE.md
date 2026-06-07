# CLAUDE.md — Coding Rules & Conventions for MEET_RUDI

> Machine-loaded each session. The *why/what* lives in [project.md](project.md); this file is
> the *how*. When product context is needed, read project.md. Last updated: 2026-06-04.

---

## 0. Prime Directives (never violate)

1. **No PII leaves the AWS EU plane.** The neo-cloud GPU plane and any 3rd-party AI API receive
   only **minimized, pseudonymized** payloads (opaque IDs, never names/contact/free PII).
2. **Not a medical device / no medical advice.** Never produce diagnostic or treatment output.
   Route medical-advice requests and red-flag content through the guardrail layer (§6).
3. **EU residency only.** Every datastore, queue, bucket, model endpoint, and 3rd-party service
   must be EU-region and covered by a DPA. If unsure, ask before wiring it in.
4. **Everything swappable is behind an interface** — WhatsApp provider, model gateway, storage.
   No vendor SDK calls in business logic.
5. **Secrets never in code or git.** Use AWS Secrets Manager / SSM Parameter Store; reference by
   name. No tokens, keys, or numbers in source, fixtures, or logs.

---

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
  /docs            # project.md lives at root; deeper docs here
  ```
- **Python:** 3.12+, type hints required, `ruff` (lint+format), `mypy` for typed packages,
  `pytest`. Prefer `pydantic` models at boundaries.
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
  Backed by Postgres+pgvector now; the interface must not leak pgvector specifics.

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
- **Fargate** for long-running orchestrator/workers/serving shims.
- **Async-first:** prefer SQS/event decoupling over synchronous chains; the conversation can
  tolerate 10–15s.
- Every resource: **EU region, tagged** (env, service, cost-center), least-privilege IAM.

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
- Prefer extending an existing abstraction over adding a new vendor dependency; if a new
  dependency or service is needed, flag EU-residency + DPA implications first.
- When product intent is unclear, ask — don't assume. Keep the Decisions Log in project.md
  current as choices are made.
