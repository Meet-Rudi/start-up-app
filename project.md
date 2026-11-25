# MEET_RUDI — Project Blueprint

> Human-facing blueprint (the *why* and *what*). Coding rules and conventions live in
> [CLAUDE.md](CLAUDE.md) (the *how*), which Claude Code auto-loads each session.
> Last updated: 2026-11-13.

---

## 1. Mission & Non-Goals

> **Pivot (2026-10-28).** MEET_RUDI is becoming a **multi-tenant conversational platform**:
> tenants configure clones from versioned skills and consume them through our API, as a
> white-label service, or on channels we run (WhatsApp, voice, chat). Coaching comes first; any
> legal purpose later. The diabetes lifestyle coach described in this file continues as
> **tenant #1 (CM)**. Platform design: [docs/platform/blueprint.md](docs/platform/blueprint.md);
> decisions #24–#45 below. Sections 2–10 still describe the CM service until they are rewritten.

**Mission.** MEET_RUDI ("Rudi") helps people adhere to a prescribed *lifestyle* (movement,
diet, daily activity, and related habits) in order to support their recovery or treatment.
Rudi does this by engaging each person in regular, natural conversation — feeling and
behaving like one of their everyday chat buddies on WhatsApp.

**How we help.** Ongoing, AI-generated conversation that:
- asks about and reflects on the user's activity, movement, diet, and lifestyle;
- remembers each user's case and how it develops over time (long-term memory);
- understands photos of food (computer vision) and audio messages;
- (later) ingests data points from smart wearables.

### Non-Goals / Hard Guardrails
- **Not a medical device.** We do not diagnose, treat, or make clinical claims. Design so
  this line is never *accidentally* crossed. ("Not for now" is an explicit product stance,
  not a deferred ambition.)
- **No medical advice.** Rudi supports adherence to a plan a clinician already set; it does
  not invent medical recommendations. When a user asks for medical advice or reports
  red-flag symptoms, Rudi deflects and (where applicable) escalates — see §7 Guardrails.
- **No unsolicited mass messaging.** All proactive contact respects WhatsApp policy and the
  user's opt-in (see §4).

---

## 2. Users, Pilot & Consent

- **First cohort:** Type-2-diabetes (T2D) patients via a **pilot organization**, primarily in
  **Belgium**. T2D lifestyle adherence (diet, movement, glucose-related habits, medication
  adherence) is therefore our **first guideline domain**.
- **Onboarding model:** the **user reaches out first** — they add/invite the Rudi account as a
  contact and send the first message. That first inbound message opens the initial
  conversation window.
- **Consent:** health data is **special-category data** (GDPR Art. 9). Enrollment must capture
  explicit, informed, revocable consent before any health-related processing. Consent state
  is part of the user record and gates processing.
- **Relationship ownership:** for the pilot, the patient relationship is mediated by the pilot
  organization; clarify data-controller vs data-processor roles in the DPA. *(Open: confirm
  controller/processor split with the pilot org.)*

---

## 3. Product Model — The Rudi Experience

- **Channel:** WhatsApp is the primary (initially only) channel. Rudi should feel like a
  regular chat buddy: text, images, and audio messages.
- **Personas:** several "Rudis," each on its **own WhatsApp number**, differing by *persona*
  (not for throughput). **A user is pinned to one Rudi/number for life** (barring explicit
  migration) — required for memory continuity and per-number quality-rating management.
- **Proactive cadence:** Rudi initiates **~2–3× per week** when the user has gone quiet;
  otherwise it responds reactively. (See §4 for the WhatsApp constraint this rides on.)
- **Language:** **English-first for the pilot**, but built **i18n-ready** from day one
  (no hard-coded user-facing strings; locale on the user profile; prompts & templates keyed
  by language) so Dutch/French is a content task, not a rewrite.

---

## 4. WhatsApp Constraints (load-bearing — read before designing the conversation engine)

WhatsApp Business does **not** allow free-form proactive messaging. The conversation engine
therefore has two explicit modes:

- **In-window (free-form):** within **24h** of the user's last inbound message, Rudi can send
  rich, free-form, full-persona replies.
- **Out-of-window (templated):** after 24h of user silence, Rudi may send **only pre-approved
  template messages**. Proactive check-ins use a lightweight templated "tap on the shoulder"
  ("Rudi has a question for you 👋 — reply to continue"); the rich conversation resumes once
  the user replies and reopens the window.

Other rules baked into the design:
- **Quality rating & messaging tier are per-number.** Reports/blocks degrade a number → it
  gets throttled or banned. We protect each number's rating; we do **not** spread users across
  numbers to evade limits.
- **Access path:** start via a **BSP (Twilio-like)** for the PoC; migrate to **Meta Cloud API
  direct** after PoC. The WhatsApp provider is hidden behind an interface so this swap touches
  no business logic (see CLAUDE.md).

---

## 5. High-Level Architecture

Two cooperating planes:

### App / Orchestration Plane — AWS (EU region)
- **Event-driven & async.** Relaxed latency (10–15s acceptable) lets us use queues/events
  (SQS), batching, and tolerate slower/cheaper inference.
- **Compute:** **Lambda** (container images) for webhook ingest and short tasks; **Fargate**
  for long-running work (orchestrator, model serving shims, workers).
- **Datastore (PoC):** a **single Postgres (RDS/Aurora) with `pgvector`** holding user profile,
  event log, and vectors. Split out a dedicated vector DB only when recall volume demands it.
- **Source of truth** for the user "case": the structured profile + event log in Postgres;
  vectors and cached summaries are derived/rebuildable.

### Inference / GPU Plane — Neo-cloud (EU region)
- AWS will not grant GPU quota to a new startup, so self-hosted GPU inference runs on a
  **neo-cloud** (CoreWeave / Lambda / RunPod / etc.), provisioned via its own IaC/API.
- **PII boundary:** PII/health data **stays in the AWS EU plane**. The GPU plane receives only
  **minimized, pseudonymized** payloads (IDs not names). EU-region GPU + signed DPA required.

```
WhatsApp ⇄ BSP(Twilio→Meta) ⇄ [AWS EU: webhook(Lambda) → queue → orchestrator(Fargate)
                                 → memory(pgvector) → model gateway] ⇄ [Neo-cloud EU GPU:
                                 embeddings, self-hosted reasoning model]  (pseudonymized only)
                                                     │
                                          3rd-party LLM/ASR/vision APIs (EU-eligible)
```

---

## 6. AI Architecture

- **Orchestration:** **own orchestration agents**, framework-light. Reconsider a framework
  (e.g. LangGraph) only if real pain emerges.
- **Per-message pipeline (conceptual):** ingest → (ASR if audio / vision if image) →
  context assembly (profile + recent summary + vector recall) → reasoning LLM →
  **guardrail gate** → send (respecting window mode).
- **Memory (three layers):**
  1. **Structured user profile** — goals, constraints, plan, locale, consent, key facts.
  2. **Cached recent state / summary** — short rolling summary of recent issues & state,
     cheap to inject into prompts.
  3. **Vector store** — semantic long-term recall of past conversation/events.
- **Model strategy — phased, behind a gateway:**
  - PoC: **reasoning on 3rd-party API** (cheap to start, zero ops).
  - Then move **embeddings** to self-hosted (steady, high-volume, easy win).
  - Move **reasoning** to a self-hosted open-weight model on neo-cloud **only when utilization
    justifies the always-on GPU cost**. Frontier-grade is not required — good guardrails carry
    quality for lifestyle conversation.
  - A **model-agnostic gateway** lets us swap **API ↔ self-hosted** per workload without
    touching business logic.

---

## 7. Guardrails ("double-checking against guidelines" without giving medical advice)

Layered, automated-first:
1. **RAG grounding** over a **curated guideline corpus** (starting with T2D lifestyle guidance)
   to keep responses consistent with vetted material.
2. **ML classifiers on key risk aspects** — detect e.g. hypoglycemia, disordered-eating
   signals, and other red-flag content → trigger deflection/escalation and suppress
   advice-like output.
3. **Knowledge graph** — used **only when a case genuinely needs** structured reasoning;
   not a default dependency.
4. **Medical-advice refusal pattern** — when asked for diagnosis/treatment, Rudi stays in the
   adherence-support lane and points the user back to their clinician.
5. **Periodic human spot-checks** of sampled conversations for safety/quality (operational,
   not in the live send path).

---

## 8. Compliance Posture

- **GDPR-first**, EU data residency; AWS EU region; neo-cloud EU region.
- Health data = **Art. 9 special-category** → explicit consent, data minimization, purpose
  limitation, right to erasure honored across all stores (incl. vectors & caches).
- **PII never leaves the AWS EU plane** except as pseudonymized payloads to the EU GPU plane
  under a DPA.
- DPAs required with: BSP (Twilio/Meta), any 3rd-party AI API used, and the neo-cloud vendor.
- *(Open: data-controller/processor mapping with the pilot org; DPIA for the pilot.)*

---

## 9. Cost Model & Unit Economics

- **Revenue:** ~**€20 / user / month** (expected).
- **Cost ceiling:** **≤ €13 / active user / month** (AI + infra), targeting **~€7 margin**.
- **Risk:** *heavy* users (lots of images/audio, long chats) blowing past the average — not the
  ceiling itself. Mitigate with caching, batching, right-sized models, and per-user cost
  observability.
- **Why self-hosting is phased:** an always-on GPU is *more* expensive per user at low volume;
  the economics flip only at high, steady utilization. API-first protects early unit economics.

---

## 10. Roadmap / Phasing

- **Phase 0 — Auxiliary side deliverables** (precede the main MVP; to be specified next).
- **Phase 1 — PoC:** text-first WhatsApp loop via Twilio, single persona, English, memory
  (pgvector), reasoning on API, basic RAG guardrail, EU AWS.
- **Phase 2 — Pilot:** T2D cohort; add audio (ASR) + food vision; risk classifiers; templated
  proactive cadence; cost observability; consent/DPIA in place.
- **Phase 3 — Scale & cost:** self-host embeddings, then reasoning on neo-cloud GPU; Meta Cloud
  API direct; multi-persona; Dutch/French localization.
- **Phase 4 — Wearables:** integrate device data points.

---

## 11. Decisions Log (settled)

| # | Decision |
|---|----------|
| 1 | WhatsApp via **Twilio (BSP)** first → **Meta Cloud API direct** later; hidden behind a provider interface. |
| 2 | **Multi-persona**, one number per persona; **one user → one fixed Rudi for life**. |
| 3 | Proactive cadence **~2–3×/week**; in-window free-form vs out-of-window templated modes. |
| 4 | **GDPR-first**, EU residency, AWS EU region + neo-cloud EU region. |
| 5 | **Not a medical device / no medical advice** — explicit product guardrails. |
| 6 | Stack: **Python** (AI/messaging core) + **TypeScript/JS** (front-end). **Mono-repo.** |
| 7 | **2 senior engineers**, greenfield. |
| 8 | AWS **SAM/CloudFormation**, **Lambda container images**; CFN/Terraform for Fargate; neo-cloud own IaC. |
| 9 | **Own orchestration**, framework-light. |
| 10 | Memory = **pgvector + cached summary + structured profile**; Postgres is source of truth. *(Vectors superseded by #39.)* |
| 11 | **Self-host embeddings + mid-size open-weight reasoning model** for cost; **API-first**, phased. |
| 12 | **Relaxed latency** (10–15s) → async/queue-based design. |
| 13 | **English-first, i18n-ready**. |
| 14 | Guardrails = **RAG + ML risk classifiers + (optional) knowledge graph** + human spot-checks. |
| 15 | **Neo-cloud for GPU** (AWS denies GPU quota to new startups); **PII stays in AWS EU plane**, pseudonymized payloads to GPU plane. |
| 16 | Unit economics: ~€20 revenue, **≤ €13 cost/active-user/mo**, ~€7 margin. |
| 17 | **Erasure stays operator-run.** A frozen conversation (objection gate) raises a case for a human; it never triggers deletion. What someone says in a chat is too brittle a basis for deleting their data. |
| 18 | **Training Arena** (docs/training-arena.md): mass-simulated conversations with the **deployed** Rudi engine via `meetrudi-engine-turn` — never a copy of Rudi. Its own stack, bucket and role; synthetic inputs only. |
| 19 | The arena runs on **its own model cascades and keys**, never live Rudi's quota. Tester and judge: **own Groq account, free tier first**, OVH as fallback, with preventive failover before a known limit is reached. |
| 20 | Arena **budgets per case, batch, user-day and month**, counting model calls **and an assumed AWS overhead** unit price; a budget stop is its own outcome (`over_budget`). |
| 21 | **Arena Generator — Prime Directive 3 exception, approved 2026-10-15.** Batch files may be written by Claude Code on a super-admin's own computer under their **Claude subscription** (Anthropic, US, consumer terms — no DPA). Allowed only because nothing personal is sent: the skill description (scanned for identifiers first), the format rules and the persona sheet — never Rudi's prompts, transcripts, results or keys. No API key is used; nothing runs in AWS. |
| 22 | **Improvement Extractor — exception 21 extended, requested 2026-10-15.** Any arena user may have an arena **results log** read by Claude Code on their own computer under their Claude subscription, to draft improvement work (application errors, Rudi's weak points, feature requests). Sent: the results log — **synthetic** conversations, judge verdicts and recorded errors only, with e-mail addresses, phone numbers and IBANs masked before sending — and the **skills catalogue** (`services/training-arena/seed/skills/rudi-skills.yaml`, which describes Rudi's behaviour and code locations). Never Rudi's prompts, live conversations or keys. Without the local generator the analysis runs in AWS on the arena's own EU cascade (OVH, DPA), counted in the arena's month budget. |
| 23 | **First contact — target behaviour (Gym 01), agreed 2026-10-23.** Rudi may give recipes and training schedules and answer general nutrition questions for people with diabetes (anything a personal trainer, dietitian, sleep or habit coach may say); medication, insulin, supplements, diagnoses and reading glucose or lab values stay out. Distress (divorce, stress, grief) is coached with empathy; only risk signals (suicidal thoughts, self-harm, chest pain, stroke or severe hypo signs) stop coaching, with 1813 / 112. The first conversation ends with a step the user chose — offered, never forced. This is the target the arena measures against; the coaching prompts change through feature requests and evals, not by this entry. Skills are trained from **skill briefs** (`rudi-skill-brief@1`). A remodel of the skills catalogue is planned. |
| 24 | **Platform pivot, approved 2026-10-28.** Rudi becomes a multi-tenant conversational platform: tenant → clone → immutable clone versions (draft → staging → live), several clones per tenant, several personas per clone with each end-user pinned to one. Coaching first, any legal purpose later; first tenant types: corporate wellbeing/HR, fitness and lifestyle, adult and workplace education. **We are processor for every tenant.** CM becomes tenant #1. Design: `docs/platform/blueprint.md`. |
| 25 | **Skill model.** Three kinds: **real-time** (inside the engine process, every turn), **turn** (own Lambda, called in parallel within the turn deadline, skipped when late), **background** (own Lambda, after the reply or on a schedule). One envelope (`SkillRequest`/`SkillResult`, optional `context` dictionary). Runtime package and training brief are separate formats linked by `slug@version`. Semantic versions; clones pin a major, minors roll out after the skill brief passes. |
| 26 | **Clones are configuration, skills are code.** A clone built from existing skills, persona and psychometric tuning, settings and knowledge are console actions with no deploy. A new skill, or a skill gaining or losing a tool or sub-agent, ships through `deploy.py`. New skills are **minted** from the console: scope request → legal vetting by the team → inventory match % → arena rounds until the release gate → added to the clone. |
| 27 | **Policies are skills.** Acceptable-use exclusions (minors, EU AI Act high-risk uses, financial/legal/debt advice) and safety behaviours ship as a default-included **Baseline pack**, removable or changeable only with a contract addendum, our review and an arena gate. A mandatory, non-removable baseline is deferred. |
| 28 | **Tiered isolation, one AWS account.** Pooled cell by default, dedicated cells (own Lambdas with reserved concurrency, queue, database, KMS key) as premium. Stages by name. Resources: `meetrudi-internal-{stage}-…` (platform-managing) and `meetrudi-tenant-{stage}-{cell}-…` (tenant traffic). |
| 29 | **Identifiers.** `{cat}_` + 32 lowercase Crockford base32 characters; three-letter prefixes registered in `packages/rudi_platform/ids.py` and `docs/platform/conventions.md`. End-user ids are derived per tenant (HMAC of the tenant salt and the channel handle), so identities never link across tenants. |
| 30 | **PlanetScale Postgres is the platform's system of record — Prime Directive 1 exception, approved 2026-10-28.** eu-central-1, row-level security for pooled tenants (vectors: #39, not pgvector), a database per dedicated cell. PlanetScale's DPA states its primary processing facilities are in the US; accepted under the DPA and the 2021 SCCs, and listed as a sub-processor for every tenant. Aurora rejected (resource throttling, Data API limits). *(Engine: PlanetScale Vitess, MySQL 8, since #46; row-level security replaced by #47. The Prime Directive 1 exception stands.)* |
| 31 | **Memory is pluggable per use case.** The legacy S3 store keeps serving CM until its replacement is live; S3 **fast-memory snapshots** serve latency-critical real-time skills; Postgres is the record. Derived stores are rebuildable and covered by erasure. *(The record is PlanetScale Vitess since #46.)* |
| 32 | **13-minute rule.** No Lambda run works past 13 minutes; long work hands over (checkpoint, then async self-invoke) from 12 minutes, using `rudi_platform.runtime`. Every skill accepts an optional context dictionary that a successor run inherits. |
| 33 | **Roles are data.** Permission catalogue, roles scoped to platform or tenant, assignments per tenant. Seed roles: platform super-admin, platform support, platform skill tester, tenant admin, clone designer, tenant operator, tester. Super-admin defines new roles and delegates their assignment to tenant admins. |
| 34 | **Coexistence and sequencing.** Today's CM service stays fully working at every step; a component is replaced only after its successor is live and holds the data. A basic console moves into Phase 2 and runs on CM as the live demo for onboarding further tenants. |
| 35 | **Platform IAM roles under a permissions boundary.** Once `meetrudi-boundary` and `meetrudi-deployer-platform-roles` exist (console), templates may create `meetrudi-internal-*` / `meetrudi-tenant-*` roles carrying that boundary. Legacy roles, managed policies and the boundary stay console-only. |
| 36 | **CM clone version 1 = today's CM, proven by parity, 2026-11-03.** CM's first clone version reproduces the live WhatsApp engine exactly (same prompt text and order, journey, limits, personas, documents); `test_engine_parity.py` compares the two turn by turn and is the release gate of the 17 skill packages' version 1.0.0 (recorded as gate `legacy-parity` in the audit log, not an arena gate). Consequences: `acceptable-use` is left out of CM v1 (it would change CM's prompt; CM v2 adds it after an arena batch); the crisis helpline stays Belgian as a clone setting. The engine de-identifies at the conversation boundary (inbound scrubbed before storage and model; names restored only in the text the person reads), as CM's channels do today, instead of through the gateway's mask-and-restore, which would store replies with real names. **Confirmed 2026-11-03.** |
| 37 | **CM's calls join the platform now, 2026-11-04.** The Voice call pack (4 skills) and the engine's call support are pulled forward from Phase 2; CM clone v1 serves WhatsApp and calls, and `test_voice_parity.py` holds its calls to the live call brain (`services/call`) turn by turn. v1 keeps today's call behaviour, quirks included, as visible settings to remove in v2 after an arena run: the speaking-style block's position in the learn stage, no time note in learn, and **no AI-disclosure instruction in calls that start in learn** (every first call). Live findings, not yet fixed: that disclosure gap; calls ignore the `max_seconds` callers send (the brain reads `max_minutes`, so every call runs on the 12-minute default); calls coach from `health-coaching-guidance.md` while WhatsApp uses `diabetes-t2d-guidance.md`. |
| 38 | **CM's live calls run on the platform engine, 2026-11-05.** `services/call` carries both engines (`platform_call.py`): each call is fixed to one when answered (`manifest.engine`), from its config's `engine` or the service default `CALL_ENGINE`, **`platform` since 2026-11-05** (`legacy` + redeploy rolls back); a platform that cannot load or open sends the call to legacy. The engine ships as the layer `meetrudi-tenant-prod-pool-engine`. Release evidence instead of a phone test (the user does not speak Flemish): 12 whole simulated calls against the deployed stack, both engines, EN and NL, every call goal (`rudi_sim_call.py`); 11 recorded voice checks, real Twilio calls to the account's own spare number, each voice heard by Whisper in the language it was set up for and saying the given text, voice/language switches included (`rudi_voice_check.py`; only calls to an own number are ever recorded). The call disclosure gap (#37) stays as it is for now: the existing-solution exception covers it (user, 2026-11-04). Found while testing, fixed in both engines: the 250-token reply cap returned nothing on every turn since gpt-oss thinks first (now 1024), and a bracketless name alias was spoken as "Person A" (now restored). Platform calls try `ovh-gpt-oss-120b` and Llama last (both overran the 7 s attempt). Open: Groq's free tier (8,000 tokens a minute per model, about three call turns) is the binding limit on call latency (USER-TODO #4); since names are de-identified (09-16) models sometimes invent the caller's name ("Jan"), on both engines; OVH's gpt-oss-20b sometimes trips the leak guard; Twilio rejects the dispatcher's status-event list (21626), so only `completed` callbacks arrive. |
| 39 | **Vector memory only when a skill needs it; then Qdrant, with its metadata in PlanetScale, 2026-11-05.** Conversation memory stays **S3 raw history + the pre-built, regularly updated "recent state" record** (fast memory, #31) for every skill, until a skill shows this is not enough (e.g. guardrails over complex medical procedures). Vector memory is more complex and costly to run, so a skill adopts it only with a recorded reason. When one does: **Qdrant Cloud, free cluster, is the norm**, vectors of at most 2,048 dimensions. Every vectorized chunk has a **metadata record in a dedicated PlanetScale schema**; metadata filtering runs there, independently of the vector search, and **access rights are enforced by matching on the metadata**. Both stores are multi-tenant: the **tenant identity hash** is in every metadata record and every Qdrant point. Vectors are derived and rebuildable from the record and covered by erasure (#31); skills reach them only through `MemoryStore`. A free cluster is decommissioned when unused, so a scheduled keep-alive ping runs during early testing, until daily/weekly traffic does the same. Replaces pgvector (#10, #30) as the planned vector store. **Prime Directive 1/3:** Qdrant is a second datastore outside our AWS account, approved in principle here; it is wired in only once the cluster is in an EU region and Qdrant's DPA is signed, and it is then listed as a sub-processor. |
| 40 | **CM's WhatsApp runs on the platform engine, 2026-11-05.** `services/whatsapp` carries both engines behind one function, `chat_engine.respond`, used by the processor, the tester console's chat and the arena door; `CHAT_ENGINE` in the template's Globals picks one, **`platform` since 2026-11-05** (`legacy` + `python deploy.py whatsapp` rolls back; an environment value, because a deploy keeps a stack parameter's previous value). Both engines read and write the same `meta.ai_state`, so a conversation may change engine at any message. A platform that cannot load or breaks on a message answers that message on legacy; model failures stay the legacy gateway's errors. The service ships its own layer copy, `meetrudi-tenant-prod-pool-engine-whatsapp` (one builder for both services: `packages/build_engine_layer.py`), so each deploy carries the engine its adapter was tested with. Nothing around the engine moves: de-identification, the objection gate, delivery, scheduling, summaries and reach-out stay legacy until the scheduler (Phase 2); a halt by the platform's own gate freezes the contact as the processor's gate would. The arena door takes `engine` per event and otherwise follows `CHAT_ENGINE`, on the arena's own keys for both engines. Rollout: all ten WhatsApp contacts but one stray are registered testers, so switching the service is switching the testers. Release evidence, besides the parity test: simulated conversations through the deployed door on the arena's keys (`rudi_sim_chat.py`). Platform: 4 conversations, 24 turns, English, Dutch and an English-to-Dutch switch, one of them on the live default — learn → goal → commit → check-in reached, every reply in the person's language, names restored, no failed turn and no blocked reply. Legacy for comparison: 3 conversations, 20 turns, the same course (one turn lost to the arena's models). Not switched: the operator test console (it predates de-identification) and `rudi-chat`. Same day, calls: the call brief now says the name is a placeholder to copy exactly (`call-goals-and-whatsapp-handoff` 1.1.0, both call engines): 0 invented names in 64 simulated call replies against 2 in 40 before, the name used in 66% of replies against 38%; platform turn records now say why a reply was blocked (`guard`). That showed OVH gpt-oss-20b's blocked replies are prose instead of the JSON envelope, carrying internal field names (3 of 10 English disclosure turns on the platform became the redirect when Groq was exhausted). Fixed the same day in both call engines: a call turn's answer without the envelope fails over to the next model (`require_reply`; WhatsApp and the opening unchanged). Evidence: a call pinned to the OVH models — prose answers logged `no-envelope` and passed on; 20 call openings on the live cascade — no redirect, no failed turn, the AI disclosure delivered on all 10 per engine. |
| 41 | **Tenants are visibly apart in S3 and PlanetScale, 2026-11-09.** Isolation is something a person reading the data can see, on top of row-level security and the tenant keys. **S3:** every key that holds a tenant's data sits under the tenant's folder, **`tenants/{tenant-slug}/…`**, in every bucket, so a bucket shows one folder per tenant; objects of no tenant go under `platform/…`. The folder is the tenant's slug (e.g. `cm`), not its `ten_` id, so it reads without a lookup; a slug is therefore fixed when the tenant is created (a database trigger refuses a change) and is an organisation's short name, never a person's. Keys are built only by `rudi_platform.naming` (`tenant_s3_key`, `platform_s3_key`), which refuses segments that could climb out of the folder; lifecycle rules across tenants filter on object tags (the checkpoint store tags `retention=checkpoint`). **PlanetScale:** every multi-tenant table has its own **`tenant_id` column**, even where a parent row implies the tenant, and row-level security keys on it; the tables without one (the platform's own and the shared skill inventory) are listed by name in the schema test, so a new table needs a `tenant_id` or a reasoned entry there. #39's vector metadata records follow the same rule. Replaces the blueprint's `t/{tenant}/`. Legacy: CM's keys in `meetrudi-ai-data-…` (`conversations/{uid}/…`) keep their layout until CM's store is cut over, then move to `tenants/cm/` with the data migration (blueprint §7.3). The existing schema already met the column rule; nothing live changes. *(Since #46 there is no row-level security; #47 makes every statement name its tenant instead.)* |
| 42 | **The platform database's full schema and its migration runner, 2026-11-10.** The PlanetScale Postgres cluster exists (`planetscale-backbone`, branch `main`), with three credentials in Secrets Manager: `meetrudi/planetscale-backbone/primary-main-write`, `…/primary-main-read`, `…/replica-main-read`. They hold only a user name and password; host, port and database name are deployment settings (`deploy.local.json`), since a host is not a credential. **Schema:** besides `registry`, `access_control`, `audit_trail` (0001–0002), migration 0003 adds `runtime` (end-users, channel handles, identity links, consents, conversations, turns, messages, escalations, takeovers) and `schedule` (due items); 0004 adds `metering` (metered calls, budgets) and `ops` (alerts, erasures, long jobs). The shapes follow the engine's own records (`ConversationState`, `UsageRecord`). **Personal text is ciphertext only:** message text, channel handles and conversation state sit in columns of type `public.rudi_ciphertext`, which accepts only an AWS Encryption SDK message (tenant KMS key, blueprint §6); handles are found by an HMAC with the tenant salt. Memory fields live inside the conversation state rather than in a separate `memory` schema, since the engine reads and writes them together. Usage is a plain table until volume calls for monthly partitions (partitions would need row-level security on each one). An erasure keeps only the SHA-256 of the end-user id once done. Every table has its own `tenant_id` with forced row-level security (#41). **Runner:** `rudi_platform.db.migrate`, run by the Lambda `meetrudi-internal-prod-db-migrate` (the legacy role `meetrudi-lambda-runner`, which may read `meetrudi*` secrets) through `python db/migrate.py` (`roles`, `plan`, `rehearse`, `apply`), so the credentials never leave AWS. Each migration and its `meta.migrations` record commit together, an advisory lock serialises runners, an edited or vanished applied migration stops everything, and `rehearse` runs the pending set on the real server and rolls it back. Driver: pg8000 (pure Python), TLS always verified. **Not yet:** the encryption itself (AWS Encryption SDK and per-tenant KMS keys) and the stores that write these tables arrive with the features that use them. The secret names predate `meetrudi/{plane}/{stage}/…`: today's runner role reads them, but a template-made platform role under `meetrudi-boundary` could not (USER-TODO). *(Rewritten for Vitess by #46: tables named by area instead of schemas, no domains or triggers, runner on PyMySQL with `check`, `plan`, `apply`.)* |
| 43 | **No name is a reserved word, 2026-11-11.** Names we create never collide with a language's keywords or reserved words, so nothing needs quoting and no tool misreads them. **Database:** no schema, table, column, domain, function, parameter, variable, trigger, policy, index, constraint or alias is a word in `rudi_platform.db.keywords`: every PostgreSQL keyword (unreserved ones such as `name`, `key` and `source` included), plus the reserved words of the SQL standard, Oracle, MySQL and SQL Server, since exports and BI tools read the same names. `test_schema.py` checks every migration and the runner's own table. No migration had run, so they were renamed in place: schemas `access` → `access_control`, `audit` → `audit_trail`, `usage` → `metering` (files 0001 and 0004 renamed with them); columns `name` → `display_name`; `key` → `permission_key`, `role_key`, `persona_key`; `scope` → `scope_level`; `number` → `version_number`; `at` → `occurred_at`; `action` → `action_key`; `match` → `coverage`; `granted` → `is_granted`; `source` → `recorded_via` (consents) and `requested_via` (erasures); `session` → `session_number`; `period` → `budget_period`; the runner's `number`, `name`, `release` → `migration_number`, `migration_name`, `release_tag`. **Code:** Python names we bind are never a soft keyword (`match`, `case`, `type`) or a builtin; 75 uses of `case` and one of `match` were renamed (platform tests, Training Arena). Browser scripts declare no top-level name that is a `window` property; none did. Exception: the `type` field of CM's stored WhatsApp `Message` and of the skill manifest's `Setting`, because they name keys in stored data. Python records may keep their field names (`Tenant.name`); the PlanetScale store maps them to columns. Details: conventions §7 and §8. |
| 44 | **The user applies database DDL, 2026-11-12.** Schema changes on PlanetScale stay under the user's control. Claude writes migrations and checks them (tests, Postgres parser) and may deploy the runner and run the read-only `roles` and `plan` when asked; `rehearse` and `apply` are run by the user. `python db/migrate.py apply` refuses to run without an interactive terminal and asks for a typed `APPLY`. CLAUDE.md §7, conventions §8, db/README.md. *(Since #46 there is no `rehearse` and `roles` became `check`; Claude may run `check` and `plan`, the user runs `apply`. Since 2026-11-15 the user applies migrations in the PlanetScale console, from files prepared by `python db/console_sql.py <n>` (ids minted, each ending with its `meta_migrations` record).)* |
| 45 | **Rudi's visual identity is the template for every front-end, 2026-11-13.** After the rebrand of meetrudi.eu, its colours, type, shapes and tone were read from the live stylesheet into `docs/design/rudi-brand.md` (rules) and `web/brand/rudi-brand.css` (tokens and components), with the logo, mascot and fonts in `web/brand/`. Ivory page, petrol ink, butter accent; Manrope 500 headlines in short sentences, DM Sans body; pill buttons and organic cards with a petrol edge. Where the site falls short of accessibility minimums, ours are stricter: field borders `#5E8A8D` (3:1) and focus rings that change colour on dark and butter bands. **Fonts are self-hosted** (SIL OFL): loading Google Fonts sends visitors' IP addresses to Google, against Prime Directive 3; the live site still does, which is worth fixing there too. Pages use plain HTML and CSS first. Existing pages move over when next changed. First use: the mock-up `web/mockups/clone-request.html`, where an organisation asks for its own Rudi. CLAUDE.md §1. |
| 46 | **The platform database is PlanetScale Vitess (MySQL 8), 2026-11-15.** The database `meet-rudi-backbone` (its credentials are the secrets `meetrudi/planetscale-backbone/…`) was created as Vitess, not Postgres as #30 assumed. After weighing both engines, the user keeps Vitess, for future sharding (Vitess reshards live, even inside one large tenant, which cells cannot split) and for Lambda bursts (its router pools connections; concurrent work is still capped by the transaction pool, so turns keep transactions short and do not hold a connection across a model call), and judged running Postgres the larger technical risk. Accepted costs: no row-level security (replaced by #47); no transactional DDL, so no rehearsal (the runner applies statement by statement, records progress in `meta_migrations` and resumes a failed migration at the failed statement); no triggers, stored routines, events or foreign keys (their rules move into the stores and their contract tests: a tenant's slug never changes, published clone versions and personas are immutable, the audit trail is append-only, a reference points at an existing row); no schemas or domains (tables are named by area, `registry_*`, `runtime_*`…, and each column repeats its check). No migration had run, so 0001–0004 were rewritten in place for MySQL 8: `CHAR(36)` ids with `REGEXP_LIKE` prefix checks, `DATETIME(6)` in UTC, `JSON`, InnoDB, `utf8mb4_0900_bin`, every tenant index leading with `tenant_id`. The runner moved from pg8000 to PyMySQL (TLS always verified) and its steps are `check`, `plan`, `apply`. Schema changes need a password with PlanetScale's Admin role, on a branch with safe migrations off; moving to deploy requests once `main` has safe migrations on is a later decision. #30's DPA exception is unchanged. db/README.md, USER-TODO #3. |
| 47 | **Every database statement names its tenant — hard rule, 2026-11-15.** With no row-level security on Vitess (#46), tenant isolation in PlanetScale rests on one rule that binds every Lambda, service, script and process: a `SELECT`, `UPDATE` or `DELETE` on a tenant's rows carries `tenant_id = %s`, bound to the caller's own tenant, as a mandatory top-level `AND` of its `WHERE` (each tenant table of a join or subquery its own), and an `INSERT` writes the caller's tenant into `tenant_id`. **A statement without it is not handed over for processing:** it is refused before it is sent and never reaches the database. Enforced in code by `rudi_platform.db.tenant_guard`: every PlanetScale connection is wrapped in `TenantBound` (one tenant, or the platform's own rows with `tenant_id IS NULL`) before any other code may use it. The guard checks each statement and the tenant id bound to it; refuses what it cannot read exactly (`OR`, `XOR`, `BETWEEN` or `CASE` at the top of a `WHERE`, `UNION`, comments, comma joins…); refuses upserts on tables with a global unique key; and never lets an `UPDATE` move a row to another tenant. Platform work runs tenant by tenant. Closed exceptions, each listed in code: tables holding no tenant's data (`NO_TENANT_DATA`, the same list as `test_schema.py`), four resolvers that find the tenant of a request arriving without one (an endpoint's address, an API key's hash, an invitation token, an account's role assignments; they return ids only), and the migration runner, which writes no tenant's rows (tested). Every index of a tenant table leads with `tenant_id`, apart from the resolvers' keys. Adding an exception is a decision. CLAUDE.md §0e, conventions §8, db/README.md. |
| 48 | **The adoption request form saves in steps, holds its questions on the server and keeps partial requests for follow-up, 2026-11-24.** The team's review of the single-page request mock-up (#45) found three risks: a person who stops halfway leaves nothing to follow up on, a competitor can copy the whole questionnaire from the page, and one long page will not scale as the questions grow and branch. It also lacked two topics: the **tenant's cockpit** (how involved their team wants to be, stepping in, taking over, correcting or approving Rudi's messages, alerts, coverage hours, the figures they want to see, who on their team uses it) and **bringing their own AI or tools**, asked as an optional, closed group where Rudi supplying and running the AI is the default. **Form:** eight steps, **contact details first**, each saved on "Save and continue" (or as a draft on "Finish later" / "Back"), with a progress bar and step list (`.r-progress`, `.r-steps` in the brand). **Questions on the server:** `services/prospect-intake/src/questionnaire.json` (`rudi-adoption-request@1`); the public page carries only a generic renderer, and the endpoint hands out a step only after every step before it is saved, so the page's source shows the contact step alone. Fields and steps branch with `show_if` on earlier answers; browser and server validate against the same definition. **Endpoint:** `meetrudi-internal-prod-prospect-intake` (public Function URL, the legacy `meetrudi-lambda-runner` role until the platform boundary exists), actions `open`, `start`, `save`, `load`, `resume`; the browser keeps only the request id and a token (the record stores the token's SHA-256); logs carry ids only. **Storage:** objects of no tenant (#41) in the EU data bucket: `platform/prospects/partial/prs_….partial.json` while unsent, `platform/prospects/complete/prs_….complete.json` once sent; folder, file name and the record's `state` all say which. New id category `prs_` (`Cat.PROSPECT`). A follow-up job lists `partial/`, contacts only where `follow_up.may_contact` (step 1's required tick "I agree to be contacted by the Rudi team if any crucial input is missing before my Rudi clone goes live", text and time stored as `agreements.contact_agreement`; saving after each step is stated in the step's introduction), and skips a partial with a complete twin (the runner role cannot delete yet: USER-TODO #5). **Page:** `web/adoption-request/rudi-adoption-request.html`, self-contained (brand, fonts and images inlined, the endpoint filled in by `python deploy.py prospect-intake`), with a Content-Security-Policy that allows the endpoint only. Not yet: locking the endpoint's CORS to the site's origin (`prospect_form_origin`), rate limiting, an e-mailed resume link for another device, and the follow-up job itself. web/adoption-request/README.md. |

## 12. Open Questions

- **Inference wave (platform):** EU-resident defaults for live traffic, and which model vendors
  (OpenAI, Anthropic, OpenRouter, Groq, OVH) become tenant sub-processors. Groq (US) still leads
  the live cascade. Open Prime Directive 1/3 item.
- **Team and infrastructure budget** for the platform build (assumed: two engineers plus Claude
  Code, under €1,000/month until tenant #3).
- **Meta Tech Provider enrolment** for onboarding tenants' own WhatsApp accounts through Twilio.

- Data-controller vs processor split with the pilot org; DPIA scope.
- Which 3rd-party LLM/ASR/vision APIs are EU-residency-eligible and acceptable.
- Neo-cloud vendor selection (EU region + DPA + GPU availability).
- Heavy-user cost containment policy (caps? fair-use? tiering?).
- Specification of the Phase-0 auxiliary side deliverables.
- **Google for Startups Cloud credits** — explore via **Start it @KBC / imec.istart** (likely
  Google partners → Scale/AI tier, ~$100k–$350k in credits). Eligibility: for-profit, young
  (<10 yrs), ≤ ~Series A, new to the program; partner affiliation unlocks the larger tiers.
  **Data residency is NOT set by the credits** — to stay GDPR-clean we must use **Google Cloud
  EU regions** (e.g. `europe-west1`, St. Ghislain BE) + **Assured Workloads (EU Regions &
  Support)** + org-policy location constraints, and **Vertex AI in an EU region** (the free
  AI Studio Gemini endpoint is US-processed). Verify each model's EU-region availability.
  Use credits as a **non-PII prototyping subsidy** (model eval, food-vision, batch); keep the
  patient-data plane on the AWS EU + neo-cloud stack.
