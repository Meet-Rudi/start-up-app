# Platform conventions

> Binding for all new platform code: everything under `/packages` and every stack named
> `meetrudi-internal-…` or `meetrudi-tenant-…`. Legacy services keep their current names until
> they are cut over. The design these serve is [blueprint.md](blueprint.md).
> Code that enforces them: [`packages/rudi_platform`](../../packages/rudi_platform).
> Last updated: 2026-11-12.

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
- Ids, never names or contact data, are what logs, metrics dimensions and S3 keys carry. The one
  name in an S3 key is the tenant folder's slug (§2), an organisation's short name.

### Prefix registry

**Tenancy and registry**

| Prefix | Category (`Cat.`) | Names |
|---|---|---|
| `prs_` | `PROSPECT` | An organisation's request to adopt Rudi, before it is a tenant (the public request form) |
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

### S3 keys: one folder per tenant (decision #41)

Tenants are kept apart in a way a person browsing a bucket can read:

- **Every S3 key that holds a tenant's data starts with `tenants/{tenant-slug}/`**, in every
  bucket, tenant or internal: `tenants/cm/conversations/cnv_…/snapshot.json`. Nothing of a
  tenant's is stored outside its folder. Objects that belong to no tenant start with
  `platform/`: an adoption request from an organisation that is not a tenant yet is
  `platform/prospects/partial/prs_….partial.json` until it is sent, then
  `platform/prospects/complete/prs_….complete.json`.
- Build keys with `naming.tenant_s3_key(slug, …)` or `naming.platform_s3_key(…)`, never by hand.
  They refuse empty, `.` and `..` segments, so a key cannot climb out of its folder.
- The folder is the tenant's **slug** (`registry.tenants.slug`, e.g. `cm`), not its `ten_` id,
  so it reads without a lookup. A slug is fixed when the tenant is created; the database refuses
  a change, since renaming a folder means copying every object. The display name can change.
- Below the tenant folder, keys carry ids, never names or contact data (§1).
- Code working for one tenant reads and writes inside that tenant's folder only. Offboarding and
  a tenant's export are one prefix listing. A dedicated cell's bucket policy can grant by
  `tenants/{slug}/*`.
- A lifecycle rule for one kind of object across tenants (e.g. checkpoint expiry) filters on an
  object tag (`retention=<kind>`), since the kind is no longer the start of the key. Writing a
  tag needs `s3:PutObjectTagging`.
- **Legacy:** CM's keys in `meetrudi-ai-data-…` (`conversations/{uid}/…` and the rest) keep
  their layout until CM's store is cut over, then move to `tenants/cm/` with the data migration
  (blueprint §7.3).

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
- **PlanetScale (Vitess, MySQL 8; decision #46)** is the system of record once live, reached
  only through the tenant guard (§8).
- Derived stores (snapshots, vectors, summaries) must be rebuildable from the record and covered
  by erasure.

### Vector memory (decision #39)

S3 history plus the fast-memory statement is the default. A skill adds vector memory only with a
recorded reason that the default is not enough. Then:

- **Qdrant Cloud** holds the vectors (free cluster; at most **2,048 dimensions**, so pick or
  truncate the embedding model to fit). Nothing is wired in before the cluster is in an EU region
  and Qdrant's DPA is signed (Prime Directives 1 and 3).
- **Every chunk has a metadata record in PlanetScale**, tables `vectors_*`: chunk id, `tenant_id`
  (§8), the tenant identity hash, source, and the fields that filtering and access rights match
  on. Filtering and access
  checks run on these records, independently of the vector search.
- **A Qdrant point carries no content**: its payload holds the tenant identity hash and ids only.
  Every Qdrant search is also filtered on the tenant hash, so a slip in the metadata step can
  never return another tenant's chunks.
- **Ids:** chunk ids follow §1, with a category registered when the first chunk is stored. Qdrant
  only accepts unsigned integers or UUIDs as point ids, so the point id is a UUIDv5 derived from
  the chunk id. Re-indexing is idempotent, and erasure can compute the point id from the
  metadata record.
- **Erasure and rebuild:** deleting a metadata record deletes its point. The cluster is
  rebuildable from the record, so losing a free cluster costs a re-index, not data.
- **Embeddings** go through `ModelGateway.embed()` on pseudonymized text. Skills reach vectors
  only through `MemoryStore`, never through Qdrant's client.
- **Keep-alive:** while traffic is low, a scheduled Lambda
  (`meetrudi-internal-{stage}-qdrant-keepalive`) queries the cluster so the free tier does not
  decommission it. Removed once daily/weekly traffic keeps the cluster in use.
- The API key lives in Secrets Manager as `meetrudi/internal/{stage}/qdrant`.

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
- **No name is a reserved word** (decision #43). Besides the hard keywords the languages refuse
  anyway, Python names we bind (variables, parameters, functions, methods, imports) are never a
  soft keyword (`match`, `case`, `type`) and never shadow a builtin (`id`, `type`, `input`, …;
  ruff's `A` rules). A keyword argument passed to a call (`log(..., case=…)`, `id=new_id(…)`) is
  not a binding and may keep the name a log field or API expects. In browser JavaScript, a
  script never declares a top-level name that is a `window` property (`name`, `open`,
  `status`, `scroll`, …); inside a function it may. SQL names: §8. Known exception: the `type`
  field of `services/whatsapp/src/store.py:Message` and of the skill manifest's `Setting`,
  because they name keys in stored data (CM's saved messages, `rudi-skill@1` files).

| Module | What it is |
|---|---|
| `ids`, `naming` | §1 and §2 of this file, S3 keys included |
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
| `db` | The platform database: reading a connection secret (`connect`) and the migration runner (`migrate`); the driver lives in `services/db-migrate` |
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

The platform database is **PlanetScale Vitess (MySQL 8)**, eu-central-1 (decision #46).
Migrations live in `db/migrations/`, numbered and never edited after they have run. One database,
no schemas: a table's first word names its area (`registry_`, `access_control_`, `audit_trail_`,
`runtime_`, `schedule_`, `metering_`, `ops_`). Every id column carries its registered prefix
check. No foreign keys, triggers or stored routines: the rules they would enforce are the stores'
job, held by contract tests. Rules and the seed generator: [db/README.md](../../db/README.md).

**Hard rule: every statement names its tenant** (decision #47; CLAUDE.md §0e). Vitess has no
row-level security, so this is what keeps tenants apart, and it binds every Lambda, service,
script and process that reads or writes PlanetScale:

- A `SELECT`, `UPDATE` or `DELETE` on a tenant's rows carries `tenant_id = %s`, bound to the
  caller's own tenant, as a top-level `AND` of its `WHERE`; each tenant table of a join or
  subquery carries its own `<alias>.tenant_id = %s` in the `WHERE` of its query level or in its
  own `JOIN … ON`. An `INSERT` writes the caller's tenant into `tenant_id`. An `UPDATE` never
  sets `tenant_id`.
- **A statement without it is not handed over for processing.** Every PlanetScale connection is
  wrapped in `rudi_platform.db.TenantBound` (one tenant) or `TenantBound.platform` (the
  platform's own rows, `tenant_id IS NULL`) before any other code may use it. The guard checks
  each statement and the tenant id bound to it before sending, and raises `UnscopedStatement`
  otherwise; the database never sees the statement. No raw connection or cursor reaches
  business logic, there is no bypass flag, and `UnscopedStatement` is never caught to retry
  without the condition.
- The guard reads a plain dialect so its check is exact: positional `%s` only; no comments, `;`,
  quoted identifiers, `UNION`, `WITH`, `RIGHT`/`FULL`/`CROSS`/`NATURAL` or comma joins; no `OR`,
  `XOR`, `BETWEEN` or `CASE` at the top of a `WHERE` or `ON` (inside parentheses they are fine);
  no `INSERT … SELECT` or `REPLACE`; no `ON DUPLICATE KEY UPDATE` on a table with a global unique
  key. What it cannot read, it refuses.
- Platform work across tenants runs **tenant by tenant**.
- **Closed exceptions**, each a list in code: tables holding no tenant's data
  (`tenant_guard.NO_TENANT_DATA`), the **resolvers** (`tenant_guard.RESOLVERS`: an endpoint's
  address, an API key's hash, an invitation token, an account's role assignments at sign-in;
  they find the tenant of a request that arrives without one and return ids only), and the
  **migration runner**, which changes the schema and writes no tenant's rows (tested). Adding to
  a list is a decision.

**Every multi-tenant table has its own `tenant_id` column** (decision #41):

- `tenant_id CHAR(36) NOT NULL` with the `ten_` prefix check, on the row itself, even where a
  parent row already implies the tenant (`registry_clone_versions` carries it next to
  `clone_id`). A row's tenant is read from the row, never worked out through a join. The tenant
  guard, the indexes and erasure key on it.
- **Every index of a tenant table leads with `tenant_id`**, except the resolvers' global unique
  keys. That keeps every scoped statement cheap and is the key Vitess would shard on.
- `NULL` only where it has a stated meaning: `access_control_role_assignments` (a platform-wide
  role), `audit_trail_events` (a platform event; the trail outlives an offboarded tenant),
  `metering_calls`, `ops_alerts` and `ops_jobs` (platform work).
- A table without `tenant_id` holds no tenant's data: the platform's own tables and the shared
  skill inventory. `tests/test_schema.py` and `tenant_guard.NO_TENANT_DATA` list them by name
  (the test checks the two agree), so a new table needs either a `tenant_id` or a reasoned entry
  on both lists.
- The `vectors_*` metadata records (§5) follow the same rule.

**Personal text is stored only as ciphertext** (decision #42). Message text, channel handles
(phone numbers, addresses) and the engine's conversation state go in `*_enc` `BLOB` /
`MEDIUMBLOB` columns whose check accepts only an AWS Encryption SDK message under the tenant's
KMS key, recognised by its first byte, so plain text cannot land there by mistake. A handle is
found by a keyed hash (HMAC with the tenant salt), never by its value. Columns holding S3 keys
are `*_s3_key`, checked to start with `tenants/{slug}/`. The schema test checks both naming
rules.

**No database name is a SQL keyword or reserved word** (decision #43). Tables, columns,
indexes, constraints and query aliases (and, should another engine ever be added, schemas,
domains, functions, parameters, variables, triggers and policies) are never named after a word in
`rudi_platform.db.keywords`, in any letter case. The lists are every PostgreSQL keyword (all four
categories, so even unreserved ones like `name`, `key` or `source`) and the reserved words of
the SQL standard, Oracle, MySQL and SQL Server, since exports and BI tools read the same names.
A name that would clash is made specific: `display_name` not `name`, `role_key` not `key`,
`version_number` not `number`, `occurred_at` not `at`, areas `access_control`, `audit_trail`
and `metering`, not `access`, `audit` and `usage`. Never quote, misspell or add a trailing
underscore to get past it. `tests/test_schema.py` checks every migration and the runner's own
`meta_migrations` table; a new word list or engine is added to `keywords.py`, never removed to
let a name pass. The application's Python records may keep their own field names (`Tenant.name`);
the store maps them to the columns.

**Applying migrations:** `rudi_platform.db.migrate`, run by the Lambda
`meetrudi-internal-prod-db-migrate` with `python db/migrate.py check|plan|apply`. MySQL commits
each schema change on its own, so a migration runs statement by statement with its progress in
`meta_migrations`; a failure stops the run and the next `apply` resumes at the failed statement;
drift stops everything; the runner refuses what PlanetScale cannot run before sending anything.
The database credentials never leave Secrets Manager and the Lambda. **A person applies the
DDL, never an agent** (decision #44): Claude writes and checks migrations and may run `check`
and `plan`; `apply` is run by the user and needs an interactive terminal and a typed `APPLY`.
