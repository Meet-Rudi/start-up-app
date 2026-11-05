# Rudi Platform Blueprint — v0.2

> Status: **approved with changes** (review of v0.1 completed 2026-10-28). This file is the
> source of truth for the platform design; [conventions.md](conventions.md) holds the binding
> rules (ids, names, run time). Decisions are logged in [project.md](../../project.md) §11,
> entries #24–#40. Last updated: 2026-11-05.

Rudi becomes a multi-tenant conversational platform. A **clone** is a tenant's configured
conversational agent: personas, a journey, a mix of versioned skills, channels and a model policy.
One engine runs every clone. What differs between clones is configuration, until a clone needs a
skill that does not exist yet; then a new skill is minted.

## 1. What v0.2 changed after review

| Section | Change |
|---|---|
| Run time | No Lambda works past **13 minutes**; hand-over starts at 12 (§11). |
| Behaviour changes | New **skill-minting path** from the console: scope → legal vetting → inventory match % → arena rounds → release → clone (§5.2). |
| Naming and ids | Two planes, `meetrudi-internal-*` and `meetrudi-tenant-*`; ids are `cat_` + 32 random characters (§6, conventions.md). |
| Data | **PlanetScale Postgres**, not Aurora (Aurora throttling and Data API limits) (§7). |
| Access | Roles are data: seed roles, a platform skill tester, super-admin-defined roles, delegated assignment (§8). |
| Memory and coexistence | Memory is **pluggable** per use case, including S3 **fast-memory snapshots**; CM's legacy S3 store keeps serving until its replacement is live (§7.2, §12). |
| Roadmap | A **basic console moves into Phase 2**, running on CM as the live demo (§13). |

## 2. Concepts

```text
Platform  stage: dev | prod · region: eu-central-1 (more regions in v2)
└ Cell    pool | d-<slug>  — one deployed copy of the runtime stacks
  └ Tenant (ten_)        contract, acceptable-use profile, KMS key, pseudonym salt, model policy
    ├ Accounts (acc_)    console users; roles assigned per tenant
    ├ Libraries (lib_)   uploaded knowledge + subscriptions to curated libraries
    ├ Scope requests (scp_)  desired capabilities, vetted and matched against the skill inventory
    └ Clone (cln_)       declared business purpose, branding
      └ Clone version (cvr_)  draft → staging → live, immutable once published
        ├ Personas (per_), Journey (jny_), Composition (skill@major + overrides)
        ├ Endpoints (enp_)   WhatsApp sender, phone number, widget, API key
        └ Settings           quiet hours, cadence, retention, proactive channel order
End-user (usr_)  derived per tenant (per clone by default); handles (hdl_), consent (cns_)
└ Conversation (cnv_)  end-user × clone: journey state, window, memory, persona, schedule
```

## 3. Architecture

- **Channel adapters** (`edge-whatsapp`, `edge-voice`, `edge-api`, `edge-outbound`) verify,
  normalise, deliver, and own each channel's style, window and template rules (`ChannelAdapter`).
- **Router** maps an endpoint to tenant, clone, live version and cell; applies rate limits; mints
  the end-user id with the tenant salt.
- **Inbound queue** (FIFO per end-user) for messaging; API and voice go to the engine directly.
- **Turn Engine** (`packages/rudi_platform/engine`, run in-process by the adapters' Lambdas): loads state and
  composition, runs the journey and real-time skills, fans out to turn skills, assembles the
  prompt, calls the gateway, applies reply gates, persists, emits events.
- **Model Gateway**: model policy, de-identification and restore, cascade, reply envelope,
  metering. The only way to a model.
- **Event bus → background skills**; **scheduler** claims due rows each minute and emits
  `schedule.due` for proactive turns.
- **Control plane** (internal plane): console API, registry, skill factory, Training Arena.

## 4. Skills

### 4.1 Kinds

| Kind | Runs | When | Budget | Deploy unit |
|---|---|---|---|---|
| `real_time` | inside the engine, as hooks | every turn | ms, no network | engine bundle |
| `turn` | own Lambda | within the turn, in parallel | must beat the turn deadline; skipped when late | one Lambda per skill |
| `background` | own Lambda | after the reply or on a schedule | 13 min per run, then hand-over | one Lambda per skill |

Placing a skill: no I/O and needed every turn → real-time; I/O the reply depends on → turn;
the reply does not need it → background. On live voice only real-time skills and turn skills
pre-fetched once per call run inside a turn; no tool loops.

### 4.2 Envelope, packages, versions

- One envelope for all kinds: `SkillRequest` / `SkillResult`
  (`packages/rudi_platform/skills/contract.py`), with an optional `context` dictionary.
- Two artifacts per skill, linked by `slug@version`: the **runtime package** (code + `skill.yaml`
  manifest: hooks or tool schema, settings with overridable flags and bounds, named prompt
  sections, memory fields, requires/conflicts, channels, latency class, model tier, data classes,
  permissions) and the **training brief** (`rudi-skill-brief@1`). Different formats on purpose.
- Overrides resolve skill default → pack default → tenant default → clone version → persona,
  only where the manifest allows.
- Semantic versions. Clones pin a major; minors roll out after the brief passes; majors need a
  new clone version. Each turn records the exact versions it used.

### 4.3 Packs

- **Baseline** (default-included; removing or changing a part needs addendum, review and an arena
  gate): AI disclosure with a first-message notice, crisis signposting with local helplines,
  confidentiality and leak guard, stop and wrong-number handling, acceptable-use exclusions
  (minors, EU AI Act high-risk uses, financial/legal/debt advice; education clones serve adult
  and workplace learning and never assess or decide access).
- **Conversation** (added in Phase 1): persona temperament, language mirroring, time awareness,
  scope and tone — the basics every text clone uses.
- **Coaching**: learn, first-contact rapport, goal setting and scope rejection, commitment,
  check-in loop and promise, keep-warm reach-out, profile summary, no medical advice.
- **Voice call**: call opening and AI disclosure, speech style, call brief with call goals and
  WhatsApp hand-off, closing and time budget (4 skills; voicemail screening and dial rules stay in
  the voice adapter). The voice channel profile carries what the live call brain does around
  them: a model-written greeting on pick-up, speakable replies, an 18 s turn with 7 s attempts,
  and a call that ends rather than restarts.
- De-identification is not a skill. The engine de-identifies at the conversation boundary
  (inbound scrubbed before it is stored or sent, names restored only for the person); the gateway
  does it for every other model call.
- CM's clone = Baseline + Conversation + Coaching + Voice call + diabetes guidance library + CM
  consent settings, on WhatsApp and calls. Version 1 (`packages/rudi_tenants/cm.py`) is today's
  CM exactly, held to both live engines by parity tests: without `acceptable-use` (adding it is
  version 2, after an arena batch); persona and objection gate on WhatsApp only (the live calls
  have neither); today's three call-brain quirks kept as visible settings, including the missing
  AI-disclosure instruction in learn-stage calls; real-time skills only (background skills come
  with the scheduler).

Today's 37 catalogue skills map onto these kinds as in the v0.1 review page (section 06); the
catalogue becomes an index generated from manifests and brief links.

## 5. How behaviour changes

### 5.1 Configuration versus code

| Change | Path | Gate |
|---|---|---|
| New clone from existing skills | console | smoke batch on staging, our review |
| Persona/OCEAN tuning, settings, overlays, knowledge | console, new clone version | smoke batch |
| Skill wording, thresholds, fixes | deploy, minor | brief batch, then automatic roll-out |
| Tool or sub-agent added or removed, new memory field | deploy, major | brief batch, then tenant opt-in |
| New skill | minting path (5.2) | arena rounds until the release gate |
| Relaxing a safety default | addendum, then console | review and arena gate |

### 5.2 Minting a new skill

1. **Scope.** In the console, the tenant describes what the clone must do → scope request
   (`scp_`), one line per capability.
2. **Legal vetting.** The platform team checks the scope against the acceptable-use profile,
   high-risk uses and data classes, and approves, narrows or rejects it.
3. **Inventory match.** The console scores each capability against the skill inventory (manifest
   summaries and expected behaviour) and shows a percentage per capability and overall:
   covered by an existing skill, covered with overrides, or a gap. It advises minting where
   there is a gap.
4. **Mint.** Each gap becomes a mint request (`mnt_`). A skill brief is drafted, then arena rounds
   repeat (batch → results → Improvement Extractor → implementation by Claude Code → deploy to
   dev) until the brief's release gate passes (`gat_`). Platform skill testers run these rounds.
5. **Compose.** The released skill joins the clone in a new clone version: staging smoke batch,
   review, live.

## 6. Tenancy and isolation

- Cells: `pool` (shared) and `d-<slug>` (dedicated: own Lambdas with reserved concurrency, queue,
  database and KMS key), same templates, one AWS account. Early customers likely get dedicated
  cells; pooled is the default for scale.
- Names: `meetrudi-internal-{stage}-{component}` for the platform-managing plane,
  `meetrudi-tenant-{stage}-{cell}-{component}` for everything serving tenant traffic
  (conventions.md §2).
- Ids: `{cat}_` + 32 Crockford base32 characters; end-user ids derived from the tenant salt
  (conventions.md §1).
- Pooled isolation: `tenant_id` on every row with Postgres row-level security; S3 under
  `t/{tenant}/` with the tenant key; message text and memory values envelope-encrypted with the
  tenant key (offboarding = key deletion + row deletes); ids only in logs.
- Fairness: rate limits per tenant and clone at the router; spend tracked and alerted per tenant,
  clone and heavy end-user, plus an optional tenant kill switch; reserved concurrency for
  dedicated cells; FIFO groups per end-user.

## 7. Data

### 7.1 System of record: PlanetScale Postgres (decision #30)

PlanetScale Postgres in eu-central-1. Chosen over Aurora Serverless v2 (resource
throttling, Data API query-length and execution limits) and DynamoDB (no relational queries for
the console, no vectors). **Prime Directive 1 exception:** PlanetScale's DPA states its primary
processing facilities are in the US; it is accepted under the DPA and the 2021 SCCs and listed as
a sub-processor for every tenant. One database per dedicated cell; database branches for
development. All SQL stays plain Postgres behind the store interfaces.

| Data | Store |
|---|---|
| Registry (tenants, clones, versions, skills, endpoints, accounts, roles) | Postgres `registry` |
| Conversation state | Postgres `runtime` |
| Messages | Postgres (recent, encrypted) + S3 archive |
| Memory fields | Postgres `memory`, plus fast snapshots (7.2) |
| Schedules | Postgres `schedule`, indexed on `due_at`, claimed with `FOR UPDATE SKIP LOCKED` |
| Usage | Postgres `usage`, monthly partitions |
| Vectors (only for skills that need them, decision #39) | Qdrant Cloud; each chunk's metadata in Postgres `vectors` |
| Documents, transcripts, snapshots, checkpoints | S3 `t/{tenant}/…`, tenant key |
| Audit | Postgres append-only, exported to S3 |

### 7.2 Memory is pluggable

No single memory backend. Each skill's manifest names the store that fits its latency and
access pattern, behind `MemoryStore`:

- **Legacy S3 store** (today's `conversations/{uid}/…` layout) keeps serving CM, and the new
  engine can run CM on it through an adapter, so moving CM to the platform never waits for the
  database.
- **Fast memory:** a background skill writes a pre-built statement per conversation (profile,
  recent summary, commitment, key facts) to S3 after each turn. Real-time skills read it with one
  GET and warm Lambdas cache it with a version check; a voice call reads it once at call start.
- **Postgres** is the record once live. Snapshots and vectors are derived, rebuildable and covered
  by erasure.
- **Vector memory, only when a skill needs it** (decision #39). S3 history plus the fast-memory
  statement is the default for every skill. A skill that shows it is not enough (e.g. guardrails
  over complex medical procedures) gets vectors in **Qdrant Cloud** (free cluster, at most 2,048
  dimensions), with each chunk's **metadata record in Postgres** (`vectors` schema). Metadata
  filtering and access rights run on those records, independently of the vector search; the
  tenant identity hash is in both stores. A scheduled keep-alive ping stops an unused free
  cluster from being decommissioned until real traffic does that job. Before first use: an EU
  region, Qdrant's DPA signed, Qdrant listed as a sub-processor.

### 7.3 Keys, erasure, retention, migration

- One KMS key per tenant (`alias/meetrudi-tenant-{stage}-{ten_…}`), AWS Encryption SDK with
  data-key caching.
- Erasure API per end-user, cascading over state, memory, messages, vectors, snapshots, S3 and the
  alias vault, with a receipt. Chat-triggered deletion stays off (decision #17).
- Retention per clone, enforced daily. Offboarding purge later.
- CM data migrates (re-keyed from the global salt to CM's tenant salt) only once the Postgres
  backend is live; until then CM runs on the legacy store.

## 8. Identity and access

- **End-users:** channel-native (phone, caller id), tenant-issued JWT validated against the
  tenant's JWKS, or Rudi's login portal. All produce one `usr_` id.
- **API clients:** API key per clone and stage (hashed, shown once); signing secret per webhook.
- **Console accounts:** named users per tenant in v1 on the arena's sign-in code with a tenant
  scope, behind `IdentityProvider`; Cognito and SSO later.
- **Roles are data.** A permission catalogue (`prm_`, keys such as `clone.configure`,
  `clone.publish.staging`, `transcripts.read`, `conversation.takeover`, `keys.manage`,
  `users.manage`, `roles.assign`, `roles.define`, `usage.read`, `arena.run`, `skills.mint`,
  `scope.vet`, `breakglass.use`). A role (`rol_`) is a set of permissions with a scope (platform
  or tenant). Assignments (`rga_`) bind an account to a role, platform-wide or in one tenant.
- **Seed roles:** platform super-admin, platform support (read-only, audited break-glass),
  **platform skill tester** (runs minting rounds), tenant admin, clone designer, tenant operator,
  tester.
- **Super-admin can define new roles** from the catalogue and mark tenant-scoped roles as
  assignable by tenant admins (delegation). Every grant and use of a permission is audited.

## 9. Channels

WhatsApp (Twilio; tenant accounts via Meta Tech Provider + Twilio subaccount + Embedded Signup +
Senders API; our own numbers for Rudi-branded clones; templates from a shared library), voice
(Twilio ConversationRelay on the API Gateway WebSocket, engine in-process), chat API (sync up to
8 s, then 202 and webhook), widget (GitHub Pages for now), SMS and e-mail for outbound reach-out,
Telegram after the demo. Proactive delivery follows each clone's ordered channel list, gated by
handle, consent, quiet hours and outreach limits in the end-user's time zone.

## 10. Models and cost

One `packages/gateway`; per-call policy = tenant policy × skill model tier; platform cascade
OpenAI, Anthropic, OpenRouter, Groq, OVH (Hugging Face later); endpoints tagged with region, DPA
and retention; de-identification inside the gateway; BYOK keys under
`meetrudi/tenant/{stage}/{ten_…}/models/…`; one usage row per call; budgets track and alert, with
an optional tenant kill switch. Inference residency is a separate wave (open Prime Directive item).

## 11. Infrastructure and deployment

- One AWS account; stages by name. Raise the Lambda concurrency quota (today 10) first.
- IAM roles from templates only under `meetrudi-boundary` (infra/iam/README.md §0).
- Stacks per stage (`internal`) or per cell (`tenant`): `base`, `console`, `engine`, `edge-*`,
  `skills-turn`, `skills-background`, `scheduler`, `training-arena`. PlanetScale databases are
  provisioned in PlanetScale, not CloudFormation.
- `/packages` builds into one Lambda layer. `deploy.py` gains `--stage` and `--cell` with the
  first platform stack; legacy components deploy exactly as today.
- **13-minute rule:** `RunBudget` + `hand_over` (conventions.md §3). Long functions keep
  `Timeout: 900` as a backstop.
- Consoles and widget stay on GitHub Pages for now.

## 12. How today's pieces dock in

The v0.1 table (review page section 14) stands, with the **coexistence rule**: each piece is
replaced only after its successor is live, tested and holds the data. In particular:

- `services/whatsapp` keeps serving CM unchanged until the platform engine runs CM end to end.
- Shared code is copied into `/packages`; the services' own copies stay until each service is
  cut over, so no live import path changes underneath CM.
- Before retiring `services/ask-ai`, its seed must move: `config/ai_endpoints.json` and
  `contexts/rudi-context.md` are read live by WhatsApp, calls and the arena.

## 13. Roadmap to the demo

| Phase | Scope |
|---|---|
| 0 Ground | **Done:** conventions; `rudi_platform` core (ids, naming, skill envelope, 13-minute budget and hand-over); the platform model gateway; de-identification, leak guard, timeline and OCEAN persona with parity tests against the live copies; access model (roles as data); registry, access and audit schema with row-level security (`db/migrations`, syntax-checked, not yet run); i18n catalogue with `{persona}`. **Open:** migration runner, PlanetScale `RegistryStore` and first run on the dev database (USER-TODO #3); quota raise and permissions boundary (USER-TODO #1, #2); move the ask-ai seed, then retire the K4 services (deferred by decision). |
| 1 Tenant-aware engine | **Done:** skill manifest (`rudi-skill@1`) and composer; `RegistryStore` seam with the in-memory store and contract tests; publishing draft → staging → live with audit; journey engine (`rudi-journey@1`, checked at publish); Baseline, Conversation, Coaching and Voice call packs as 21 real-time skill packages (`packages/rudi_skills`); the Turn Engine running any clone version on WhatsApp and calls (journey variants per channel, per-channel memory and assets); `ConversationStore` seam with the legacy S3 adapter (state round-trips with CM's `ai_state`); CM as tenant #1 on both channels (clone version 1, held to the live engines turn by turn by `test_engine_parity.py` and `test_voice_parity.py`); the arena door (`engine/door.py`, the `meetrudi-engine-turn` contract plus `target: cm/rudi@live` and calls); `engine/voice.py`, the call brain's interface for the voice adapter; CM's live calls on the platform engine (decision #38: layer `meetrudi-tenant-prod-pool-engine`, `CALL_ENGINE: platform` since 2026-11-05, legacy kept as the fallback and rollback; simulated calls and recorded voice checks as the release evidence); CM's WhatsApp, tester chat and the arena door on the platform engine (decision #40: `chat_engine.respond`, layer `meetrudi-tenant-prod-pool-engine-whatsapp`, `CHAT_ENGINE: platform`, legacy as the per-message fallback and rollback; the arena door takes `engine` per event). **Open:** retire `brain.py` and `responder.py` once both channels have run on the platform for a while; deploy the door as `meetrudi-tenant-dev-pool-engine` and let the arena pick a target (needs USER-TODO #1); run an arena batch on both doors; CM version 2 (`acceptable-use`, the call disclosure gap, one guidance document). |
| 2 Channels and basic console | `edge-whatsapp`, `edge-voice` (fast memory; the engine side is done in Phase 1), `edge-api` and widget; scheduler and background skills; **basic console running on CM** (conversations, clone settings, usage, accounts and roles) as the live demo for onboarding; CM cut over to the platform engine. |
| 3 Full console and tenant #2 | Scope requests with inventory match, knowledge upload, takeover, keys and webhooks, templates, draft → staging → live; tenant #2 from existing skills, no deploy; CM data moves to Postgres when the backend is live. |
| 4 Minting and reach | The minting path end to end; first turn skill (knowledge retrieval); SMS and e-mail reach-out. |

**Demo done when:** a clone is created from existing skills with no deploy; a change goes draft →
staging → live with a passing smoke batch; that clone runs over chat/widget, WhatsApp and voice
with only its skills; CM keeps serving with history intact; a minted skill goes from scope gap to
release to a clone; usage shows per tenant, clone and skill, and one erasure call removes an
end-user everywhere.

## 14. Open items

- K3 team and budget (assumed: two engineers plus Claude Code, under €1,000 a month until tenant #3).
- Inference wave: EU-resident defaults and which model vendors become sub-processors.
- Twilio: Meta Tech Provider enrolment; Senders API is in public beta.
- Voice spike: in-process engine turn on ConversationRelay against the 1 s budget.
- Legal texts: tenant DPA, acceptable-use policy, Article 50 disclosure wording.
- PlanetScale: DPA signed, sub-processor list updated, eu-central-1 dev database created.
