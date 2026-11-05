# Platform conventions

> Binding for all new platform code: everything under `/packages` and every stack named
> `meetrudi-internal-…` or `meetrudi-tenant-…`. Legacy services keep their current names until
> they are cut over. The design these serve is [blueprint.md](blueprint.md).
> Code that enforces them: [`packages/rudi_platform`](../../packages/rudi_platform).
> Last updated: 2026-11-04.

## 1. Identifiers

Every stored object has an id of the form **`{cat}_{body}`**:

- `cat` is a three-letter category from the table below.
- `body` is **32 characters of lowercase Crockford base32**: digits and `a–z` without `i`, `l`,
  `o`, `u`. That is 160 bits.
- Example: `ten_7k2m9q4x8c1v5b3n6d0f2h4j8p1r3s5t`.

Rules:

- Mint ids with `rudi_platform.ids.new_id(Cat.X)`; validate with `require(value, Cat.X)`. Never
  hand-build an id string.
- **End-user ids are derived, not random:** `pseudonym_id(tenant_salt, channel, handle, scope)`
  is an HMAC of the tenant's salt and the channel handle. The same person maps to the same id
  inside a tenant and to unrelated ids across tenants. `scope` is the clone id by default, or
  empty when the tenant links identities tenant-wide. Handles are normalised before hashing
  (E.164 for phone numbers).
- Ids are opaque. Code may read the prefix and nothing else from them.
- Store ids as text, lowercase, exactly as minted.
- **Never rename or reuse a prefix.** Adding a category means a member in `Cat` and a row here
  in the same change; `tests/test_ids.py` fails if the two drift.
- Ids, never names or contact data, are what logs, metrics dimensions and S3 keys carry.

### Prefix registry

**Tenancy and registry**

| Prefix | Category (`Cat.`) | Names |
|---|---|---|
| `ten_` | `TENANT` | A customer organisation on the platform |
| `cel_` | `CELL` | One deployed copy of the runtime stacks (pooled or dedicated) |
| `cln_` | `CLONE` | A tenant's configured conversational agent |
| `cvr_` | `CLONE_VERSION` | An immutable published state of a clone (draft, staging, live) |
| `per_` | `PERSONA` | A face of a clone: name, avatar, OCEAN profile, voice, languages |
| `jny_` | `JOURNEY` | A stage graph a clone version follows |
| `enp_` | `ENDPOINT` | A channel endpoint: WhatsApp sender, phone number, widget, API entry |
| `tpl_` | `TEMPLATE` | A pre-approved out-of-window message template |
| `lib_` | `LIBRARY` | A knowledge library (tenant upload or curated) |
| `doc_` | `DOCUMENT` | One document inside a knowledge library |
| `mpl_` | `MODEL_POLICY` | Which models a tenant or clone may use |
| `mep_` | `MODEL_ENDPOINT` | One model endpoint in a cascade, with region and DPA tags |

**Skills and their making**

| Prefix | Category (`Cat.`) | Names |
|---|---|---|
| `skl_` | `SKILL` | A skill, across all its versions |
| `skv_` | `SKILL_VERSION` | One released version of a skill |
| `pak_` | `SKILL_PACK` | A named bundle of skills with defaults |
| `brf_` | `SKILL_BRIEF` | A training brief (`rudi-skill-brief@1`) for a skill version |
| `scp_` | `SCOPE_REQUEST` | A tenant's desired clone capabilities, matched against the skill inventory |
| `mnt_` | `MINT_REQUEST` | Development of a new skill, from scope gap to release |
| `gat_` | `GATE_RUN` | One arena gate run for a skill version or clone version |

**Access**

| Prefix | Category (`Cat.`) | Names |
|---|---|---|
| `acc_` | `ACCOUNT` | A console user account |
| `rol_` | `ROLE` | A role: a named set of permissions, seeded or created by a super-admin |
| `prm_` | `PERMISSION` | One permission in the access catalogue |
| `rga_` | `ROLE_ASSIGNMENT` | A role given to an account, platform-wide or in one tenant |
| `ses_` | `SESSION` | A console sign-in session |
| `key_` | `API_KEY` | An API key for one clone and stage |
| `whk_` | `WEBHOOK` | A tenant webhook endpoint with its signing secret |
| `inv_` | `INVITATION` | An invitation to join a tenant's console |

**End-users and conversations**

| Prefix | Category (`Cat.`) | Names |
|---|---|---|
| `usr_` | `USER` | An end-user; derived from the tenant salt and a channel handle |
| `hdl_` | `HANDLE` | One channel handle of an end-user (phone, token subject, login) |
| `lnk_` | `IDENTITY_LINK` | A tenant-approved link between end-user ids |
| `cns_` | `CONSENT` | A consent record for one end-user and clone |
| `cnv_` | `CONVERSATION` | An end-user's relationship with one clone |
| `trn_` | `TURN` | One inbound-to-reply cycle, or one proactive send |
| `msg_` | `MESSAGE` | One stored message |
| `sch_` | `SCHEDULE` | A due item: check-in, reach-out or retention job |
| `esc_` | `ESCALATION` | A red-flag event routed to a tenant contact |
| `tko_` | `TAKEOVER` | An operator taking over a conversation |

**Operations**

| Prefix | Category (`Cat.`) | Names |
|---|---|---|
| `use_` | `USAGE` | One metered model or skill call |
| `bud_` | `BUDGET` | A spend budget for a tenant, clone or end-user |
| `alr_` | `ALERT` | A raised alert (spend, errors, safety) |
| `aud_` | `AUDIT` | One audit-log entry |
| `era_` | `ERASURE` | An erasure request and its receipt |
| `job_` | `JOB` | A chain of Lambda runs doing one piece of long work |
| `chk_` | `CHECKPOINT` | A saved state one Lambda run hands to the next |
| `evt_` | `EVENT` | One event on the event bus |
| `mig_` | `MIGRATION` | One data-migration run |

Ids that are not ours keep their own shape: Twilio SIDs, AWS ARNs, Training Arena batch ids
(`TA-yyyymmdd-XXXX`). Skill **slugs** (`goal-setting`) are human names; `skl_` ids are the stored
keys. A pinned version is written `goal-setting@1.4.2`.

## 2. Resource names

Two planes, so IAM policies can grant by prefix:

| Plane | Pattern | For |
|---|---|---|
| internal | `meetrudi-internal-{stage}-{component}` | Platform-managing: console API, registry, Training Arena, skill factory, migrations |
| tenant | `meetrudi-tenant-{stage}-{cell}-{component}` | Serving tenant traffic: adapters, router, engine, skills, scheduler |

- `stage`: `dev` or `prod`.
- `cell`: `pool` for the shared cell, `d-<slug>` (1–10 of `[a-z0-9]`) for a dedicated one.
- `component`: lowercase words joined by `-`. A turn or background skill Lambda is `skill-<skill-slug>`.
- **Buckets:** `meetrudi-tenant-{stage}-{cell}-data-{account}` and `meetrudi-internal-{stage}-data-{account}`.
- **Secrets and parameters:** `meetrudi/{plane}/{stage}/…`. A tenant's own secrets go under
  `meetrudi/tenant/{stage}/{ten_…}/…`.
- **KMS:** one key per tenant, alias `alias/meetrudi-tenant-{stage}-{ten_…}`.
- **IAM grants by pattern:** `…:function:meetrudi-tenant-prod-*`, `…:role/meetrudi-internal-*`.
- **CloudFormation stacks** use the same names as their main component.
- **Length limits** (Lambda and role names 64, bucket 63, queue 80 including `.fifo`) are checked
  by `rudi_platform.naming` when a name is built, never discovered during a deploy.
- **Legacy:** resources created before the platform (`meetrudi-ai-data-…`, `meetrudi-wa-…`,
  `meetrudi-call-…`, `meetrudi-arena-…`, `meetrudi-lambda-runner`) keep their names until their
  service is cut over. Nothing new is created without a plane.

## 3. Lambda run time: the 13-minute rule

AWS kills a Lambda at 15 minutes. **No instance does work past 13 minutes.**

- Long work uses `rudi_platform.runtime.budget.RunBudget.for_lambda(context)`.
- It starts handing over at 12 minutes with `runtime.handover.hand_over(...)`: checkpoint first,
  then an asynchronous invoke of the same function with `job` and `hop + 1`.
- Functions that may run long keep `Timeout: 900` so AWS's timer is only a backstop.
- Shorter functions keep the same proportions: a 60 s function works at most 52 s.
- A job is capped at 72 hops. A longer chain is a bug.

## 4. Skills

- **Three kinds:** `real_time` (inside the engine, every turn, no network), `turn` (own Lambda,
  called within the turn, skipped when late), `background` (own Lambda, after the reply or on a
  schedule).
- **One envelope:** `SkillRequest` / `SkillResult` in `rudi_platform.skills.contract`. The
  optional `context` dictionary is passed through and patched with `context_patch`; a `None`
  value in a patch removes a key.
- **Two artifacts per skill, linked by `slug@version`:** the runtime package (code plus manifest)
  and the training brief (`rudi-skill-brief@1`). They do not share a format.
- **Versions:** semantic versioning. Clones pin a major; minors roll out after the skill brief
  passes. A change to inputs, outputs, memory fields or the set of tools is a major.
- **Manifest:** every runtime package carries a `rudi-skill@1` manifest as `skill.json` (JSON so
  Lambdas need no YAML parser and there is no build step). Its fields and rules are in
  `rudi_platform.skills.manifest`. Only what a manifest marks `overridable` can be changed by a
  clone. Internal headings and signal names go in `markers`, so the leak guard protects them.
  A stage skill lists the `outcomes` its `on_signals` may report; `memory.session` lists the
  fields cleared when a new session opens; `assets` names the clone knowledge it cannot run
  without. A prompt section with locales `["*"]` is instruction text for every language.
- **Real-time skill packages** live in `packages/rudi_skills/<slug_with_underscores>/`:
  `skill.json`, `skill.py` (`class Skill(RealTimeSkill)`, exactly the manifest's hooks) and
  `prompts/<section>.md` (or `<section>.<locale>.md`). Prompt text uses `{persona}`, never a
  persona's name, and takes its other values from settings (`{helpline_url}`). Hooks take a
  `SkillRequest` and return a `SkillResult`; a memory write outside `memory.writes` is refused.
- **Journey:** a clone version's `journey` is a `rudi-journey@1` stage graph
  (`rudi_platform.journey`): stages served by real-time skills, outcomes mapped to the next stage
  or to `concluded`. `channels` holds per-channel variants (another start, other transitions:
  a call ends when the person commits). It is checked against the skill mix at every publish.
- **Prompt slots:** skills contribute parts to fixed slots (`engine.prompt.SLOTS`: rules,
  policies, persona, style, stage, call, stage_note, budget, channel, language, check_in, time),
  ordered by `order` inside a slot. Safety text goes in `rules` or `policies`, never later.
- **Composition:** a clone version's `composition` pins `{skill, major}` with optional
  `settings`, `prompt_overrides` and `channels` (narrowing where the skill runs in this clone)
  per skill, plus `channels` and `locales`. The default language comes first.
  `rudi_platform.skills.composer.compose()` runs at every publish, and a version with errors
  never reaches staging.
- **Per channel:** a conversation's stored state holds the memory fields of the skills on its
  channel (`ComposedClone.memory_schema_for`); a clone asset may have a channel version,
  `<name>@<channel>`. What a channel does around the skills (opening, speech clean-up, time
  budget, whether a concluded conversation restarts) is its `engine.channels` profile.
- **Calls:** the voice adapter calls `engine.voice.open_call` / `call_turn` with the call
  config; the engine makes the name an alias and scrubs topic and notes before any skill sees
  them, in `request.context["call"]`.
- **Baseline pack:** `rudi_platform.skills.packs.BASELINE_SKILLS`. Removing or changing one of
  these skills turns into a review reason for the go-live reviewer.

## 5. Memory is pluggable

There is no single memory backend. Each skill names the store that fits its latency and access
pattern, behind the `MemoryStore` seam:

- **Legacy S3 store** (`services/whatsapp/src/store.py`) keeps serving CM until its replacement
  is live and migrated.
- **Fast memory:** a pre-built per-conversation snapshot in S3, written by a background skill
  and read by real-time skills in one GET, with no database round trip.
- **Postgres (PlanetScale)** is the system of record once live.
- Derived stores (snapshots, vectors, summaries) must be rebuildable from the record and covered
  by erasure.

## 6. Coexistence: CM never breaks

Every step of the migration leaves today's CM service fully working. A new component replaces an
old one only after it is live, tested and has the data. There is never a window where an existing
Rudi skill is unavailable because its successor is under construction.

## 7. Code layout and tests

- Shared platform code: `packages/rudi_platform/` (standard library only, so any Lambda can
  bundle it). Tests live in `packages/rudi_platform/tests/`:
  `python -m unittest discover -s packages/rudi_platform/tests -v`
- No pydantic in Lambda code paths for now: the zip bundles are built on Windows, and
  pydantic's compiled core would need a Linux-targeted build per function. Validate explicitly.

| Module | What it is |
|---|---|
| `ids`, `naming` | §1 and §2 of this file |
| `skills.contract` | The skill envelope (§4) |
| `runtime.budget`, `runtime.handover` | The 13-minute rule (§3) |
| `gateway` | The only way to a model: endpoints tagged by region, DPA and tier; per-call policy; cascade with 429 retries, time budget and fail-over; de-identification sandwich; one usage record per call |
| `privacy.deid` | Two-tier de-identification; persona names go in `HeuristicDetector(keep=…)` |
| `guards.leakguard` | Deterministic reply gate; skills add their own markers |
| `timeline` | Time grounding; the time zone is always passed in |
| `persona.ocean` | OCEAN scores to the temperament prompt block |
| `access` | Permission catalogue, seed roles, `AccessPolicy` decisions for checks, delegation and role definitions |
| `i18n` | The strings we author (greeting, errors, nudge…), with `{persona}` instead of a fixed name; clone overrides checked at publish |
| `skills.manifest`, `skills.composer`, `skills.packs` | The `rudi-skill@1` manifest, the publish-time check of a clone's skill mix, the Baseline pack |
| `registry` | The `RegistryStore` seam with tenant and platform contexts, an in-memory implementation, and publishing (draft → staging → live, audited; journey, personas, strings and assets checked) |
| `journey` | The `rudi-journey@1` stage graph and its check against a clone's skills |
| `memory` | `ConversationState` and its flat stored form (CM's `ai_state`), the `ConversationStore` seam, the in-memory store and `LegacyS3ConversationStore` (CM's `conversations/{uid}/meta.json`) |
| `engine` | The Turn Engine: prompt slots, channel profiles, the real-time skill base and runtime, `load_clone`, `TurnEngine`, and the Training Arena's door (`engine.door`) |

Next to it: `packages/rudi_skills` (the real-time skill packages) and `packages/rudi_tenants`
(tenant definitions kept in code until the console holds them; `cm.py` is tenant #1). Their
tests run with the platform tests.

User-facing text that is not written by the model comes from `i18n.Catalogue`, never from a
literal in code (CLAUDE.md §3).

Modules ported from `services/whatsapp/src` (deid, leakguard, timeline, the OCEAN text) are held
to the live copies by `tests/test_parity.py` until CM is cut over. A fix lands in both places.
CM's clone version 1 is held to the live engines turn by turn: WhatsApp (`webchat.chat_turn` →
`responder.respond`) by `tests/test_engine_parity.py`, calls (`ws.py` → `brain.py`) by
`tests/test_voice_parity.py`. A change to a CM prompt or rule in either place must be made in
both, or those tests fail.

## 8. Database

Migrations live in `db/migrations/`, numbered and never edited after they have run. Every id
column carries its registered prefix check; every tenant table has forced row-level security
keyed on `SET LOCAL rudi.tenant_id` / `rudi.platform`. Rules and the seed generator:
[db/README.md](../../db/README.md).
