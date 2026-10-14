# Training Arena — Blueprint (v1)

A login-gated web app for **mass-simulating conversations with the real Rudi engine**. A tester
submits a file of test cases; the arena runs each case as its own conversation, records every
message with its timing, cost and errors, and hands back results an LLM can analyse.

**Status: deployed** (2026-10-14) — `meetrudi-whatsapp` with `meetrudi-engine-turn`, and the
`meetrudi-training-arena` stack. Running a batch still needs the secrets in §14 step 3.

| Part | Where |
|---|---|
| File formats, with worked examples | [training-arena-formats.md](training-arena-formats.md), [site/training-arena/samples/](../site/training-arena/samples/) |
| The arena: API, ingest, dispatcher, case runner, assembler; SAM template; tests | [services/training-arena/](../services/training-arena/) — contract in [API.md](../services/training-arena/API.md) |
| Rudi's door for the arena: `meetrudi-engine-turn` | [services/whatsapp/src/engine_turn.py](../services/whatsapp/src/engine_turn.py), [webchat.py](../services/whatsapp/src/webchat.py) |
| The console (demo mode without `API_BASE`, live with it) | [site/training-arena/](../site/training-arena/) |
| First real batch: crisis signposting | [evals/arena-batches/crisis-signposting__input.yaml](../evals/arena-batches/crisis-signposting__input.yaml) |
| IAM files (console-only) | [infra/iam/](../infra/iam/) `meetrudi-arena-runner.*`, §7–8 of its README |

**Revision 3 (2026-10-14, as built):** AWS overhead is counted in every total and budget (§6.1);
the tester and judge run on their own Groq free-tier account first, OVH only as fallback (§5.1);
preventive failover keeps a free-tier window from running dry mid-call, and a failed step is
retried or resumed from its checkpoint (§5.5); decision models such as Jev can take over judging
(§5.6).

**Revision 2 (2026-10-14)** answers the review: results are split without holding them anywhere
(§8), the arena runs on its own model cascades and keys (§5), the tester and judge models are
named (§5.3), and every run is costed and budgeted (§6), with costs on the dashboard (§13).

Versions: **v1 chat** (this design) → v2 call → v3 WhatsApp (§17).

---

## 1. Prime-directive check (CLAUDE.md §0)

| Directive | How the arena meets it |
|---|---|
| §0.1 No PII leaves the AWS EU plane | Arena inputs are **synthetic only**, enforced at validation with the engine's own identifier detector. This matters more here than elsewhere: results are *meant* to be downloaded and fed to an analysis LLM, so nothing real may ever enter them. Arena accounts are usernames, not e-mail addresses. |
| §0.2 No medical advice | Rudi's replies pass the same guardrail prompt and leak guard as live traffic, because they come from the deployed engine. The arena exists largely to *probe* this line. |
| §0.3 EU residency | New bucket, queues and functions in `eu-central-1`. The arena may only use providers live Rudi is already cleared for (§5.4); tester and judge run on OVH Gravelines. No new vendor. |
| §0.4 Swappable behind an interface | Rudi is reached through one narrow call (`engine-turn`); the arena's own LLM use sits behind its own client; every model is an entry in a cascade file. |
| §0.5 No secrets in code | Passwords are stored hashed; the bootstrap super-admin and every arena model key live in Secrets Manager under `meetrudi/arena/`. |

---

## 2. Decisions

| # | Decision | Status | Why |
|---|---|---|---|
| D1 | Rudi's answers come from a new invoke-only function, **`meetrudi-engine-turn`, inside the existing `meetrudi-whatsapp` stack** (§4) | to confirm | The arena then holds *no copy* of Rudi. It costs one redeploy of `meetrudi-whatsapp`. |
| D2 | A **dedicated bucket** `meetrudi-arena-data-<account-id>` and a **dedicated role** `meetrudi-arena-runner` | to confirm | The arena role gets no write access to the live data bucket at all; post-analysis is one `aws s3 sync`. |
| D3 | **Request a Lambda quota increase to 1,000** before parallel runs; cap the arena at 2 parallel cases until then (§7.3) | to confirm | The account limit is **10 concurrent executions, shared with live WhatsApp and calls** (checked 2026-10-13). |
| D4 | **The arena has its own model cascades and its own keys** — never live Rudi's quota. PoC Rudi cascade: Groq → OVH, as live (§5) | **settled 2026-10-14** | A mass run must not take capacity or latency from live Rudi. |
| D5 | Tester and judge on **their own Groq account, free tier first** (`qwen/qwen3.8-27b`, `openai/gpt-oss-120b`), OVH as fallback (§5.1) | **settled 2026-10-14** | No OVH credit is spent while the free tier has room; nothing shared with Rudi. |
| D6 | Every tester reads every batch; only the submitter or a super-admin can pause, cancel or re-run | to confirm | It is a team tool; hiding results from colleagues adds friction, not safety. |
| D7 | Usernames + passwords, no e-mail, 12+ characters, 30-minute idle sessions, 10-failure lockout | to confirm | No address to store, no SES dependency; a reset is a super-admin action. |
| D8 | Batches kept until a super-admin deletes them; `inbox/` 30 days; runtime state 2 days | to confirm | Results are small and are the point of the tool. |
| D9 | Budget defaults: **€0.05 per case, €10 per batch** when the file sets none; ceilings €0.50 and €100; **€20 per user per day, €150 per month** — all counting AWS overhead (§6.3) | to confirm | Generous for real work, tight enough to stop a runaway. |
| D10 | File formats, revision 3 | to confirm | See [training-arena-formats.md §6](training-arena-formats.md). |
| D11 | Which decision model(s) to trial — eu/jev for rubric criteria, a safety classifier for crisis content (§5.6) | **open — to decide together** | All candidates ship switched off. |

---

## 3. Architecture

```
 GitHub Pages                             AWS eu-central-1
┌──────────────────────┐  HTTPS  ┌───────────────────────────────────────────────────────┐
│ site/training-arena/ │────────▶│ meetrudi-arena-api      Function URL, CORS = Pages     │
│  enter_arena.html    │         │  sign-in · users · batches · results · costs · settings│
│  arena.html          │         └───────┬───────────────────────────────────────────────┘
└──────────────────────┘                 │ async invoke                  all state in
                                         ▼                         s3://meetrudi-arena-data-…
                           meetrudi-arena-ingest
                           stream · validate · estimate cost · number the cases
                                         │ tick
                                         ▼
 EventBridge, 1/min ──▶ meetrudi-arena-dispatch.fifo ──▶ meetrudi-arena-dispatcher
                                         ▲               one tick at a time: free slots, pace,
                     "case finished"     │               budgets → releases the next cases
                     (size, outcome, €)  │                         │ one message per case
                                         │                         ▼
                                         │               meetrudi-arena-cases (SQS)
                                         │                         │
                                         └──────── meetrudi-arena-case-runner
                                                    one conversation per invocation
                                                      │                      │ lambda:Invoke
                          tester + judge cascades     │                      │ + the arena's Rudi cascade
                          Groq free tier first,       ▼                      ▼
                          then OVH (Gravelines)               ┌── meetrudi-whatsapp stack ──┐
                                                              │ meetrudi-engine-turn        │
                                                              │ de-id → responder.respond   │
                                                              │ → gateway (ARENA cascade,   │
                                                              │   arena keys) → leak guard  │
                                                              └─────────────────────────────┘
                           meetrudi-arena-assembler ◀── dispatcher, when a batch has no case left
                           plans the parts, then streams them
```

| Component | Type | Role |
|---|---|---|
| `site/training-arena/` | Static app on GitHub Pages | The console. Holds no secret and no data; every decision is the API's. |
| `meetrudi-arena-api` | Lambda + Function URL | Sign-in, sessions, users, batch submit / validate / launch / pause / cancel, results and signed downloads, dashboard figures and costs, super-admin settings, cascades, budgets, audit. |
| `meetrudi-arena-ingest` | Lambda | Streams the input file, validates it, estimates its cost, assigns case IDs, writes one case spec per case. |
| `meetrudi-arena-dispatcher` | Lambda on an SQS FIFO queue | The only writer of batch progress, the batch index, spend and daily stats. Releases cases at the configured pace and within budget. |
| `meetrudi-arena-case-runner` | Lambda on an SQS queue | Runs **one** case: drives the conversation turn by turn, prices every call, records it. |
| `meetrudi-arena-assembler` | Lambda | Plans the results parts from the batch index, then streams each part (§8). |
| `meetrudi-engine-turn` | Lambda in **`meetrudi-whatsapp`**, no URL | Answers one turn as Rudi, on the cascade the arena hands it. The only thing in this design that speaks for Rudi. |
| `meetrudi-arena-data-<account-id>` | S3 bucket | Everything the arena stores. |

New stack `meetrudi-training-arena`, source in `services/training-arena/`, shipped with
`python deploy.py training-arena`.

---

## 4. Where Rudi's answers come from

**Rule: the arena never generates, imitates or post-processes a Rudi reply.** It asks the deployed
engine for one turn and records what comes back. What the arena chooses is only *whose keys the
engine spends* (§5).

### 4.1 `meetrudi-engine-turn`

A small handler added to `services/whatsapp/src/`, deployed with the same code as the WhatsApp
processor and the tester console, so it is always the same release as live Rudi. It has no
Function URL; only the arena's role may invoke it. It is **stateless** — it reads Rudi's prompts
from the live data bucket exactly as every other Rudi function does and writes nothing anywhere.

```
request   { channel: "chat", op: "open" | "turn",
            state, vault,                      # what the previous turn returned (null to open)
            text, locale, personality, tz,
            now,                               # optional simulated clock, for `gap`
            cascade: [ {name, kind, endpoint, model, secret}, … ],   # the arena's Rudi cascade
            trace: { case, turn } }            # opaque IDs for the logs

response  { ok: true, reply,                   # as the user reads it (names restored)
            state, vault,
            info: { phase_before, phase, signals, model, lang, leak_blocked, pii_redacted },
            usage: { model, tokens_in, tokens_out, estimated, attempts: [ {model, error} ] },
            engine: { release, assets: { <prompt key>: <S3 version id> } },
            ms }
          { ok: false, error: "rate_limited" | "engine_failed" | "bad_cascade" | "bad_request", detail }
```

Inside, one turn is exactly what the tester console's chat (Track A) does today:
`deid.scrub_inbound` → `responder.respond` (guardrails, persona, phase machine, the cascade,
leak guard) → `deid.restore_outbound`. `op: "open"` returns Rudi's canned greeting, with no model
call, as a fresh conversation does. A chat turn makes exactly one successful model call — the
leak guard is a pure function — so `usage` is the whole cost of the reply.

The engine-turn function **refuses** a cascade that would touch live resources (§5.4).

### 4.2 Small, additive changes to existing code

| Change | Why |
|---|---|
| New `webchat.py` with `chat_turn()`, lifted out of `tester_api._chat_send`; `tester_api` calls it | One implementation of a chat turn for the tester console and the arena, so they cannot drift. |
| New `engine_turn.py` + one function and log group in `services/whatsapp/template.yaml` | The invoke-only entry point. |
| `gateway`: a `using(cascade)` context that replaces the S3 cascade **and drops the in-code Groq fallback** for the duration of one call; `generate()` also returns the provider's token `usage` (today it is thrown away) | The arena's keys, and nothing else, pay for an arena turn. Live callers pass nothing and behave exactly as today. |
| `responder.respond`: `info` also carries `phase_before` and `leak_blocked`; the prompt loader remembers each asset's S3 `VersionId` | The arena reports them. Nothing about a reply changes. |
| `deploy.py`: passes the git commit as a `Release` parameter | So results can say which Rudi release answered. |

All are covered by the existing 244 WhatsApp tests plus new ones: a contract test (`engine_turn`
and `tester_api` return the same reply for the same state and input) and a guard test (with an
arena cascade in force, no call ever uses a secret outside `meetrudi/arena/rudi-`).

### 4.3 What a v1 chat case does and does not exercise

- **Exercised:** the guardrail prompt, the learn → goal → commit → check-in phase machine, the
  persona block, cascade failover, JSON-envelope parsing, the leak guard, de-identification,
  language mirroring, check-in signals, the timeline (with a simulated clock).
- **Not exercised** — the chat channel has none of it, exactly like tester-console Track A: the
  inbound objection gate, Twilio delivery, 24-hour window and templates, the proactive
  re-engagement runner, profile summarising, the call stack. These arrive with v2 and v3.

Warm Lambda containers cache prompts, so a prompt edited in S3 is not seen until containers
recycle. Each result records the S3 version the engine *actually read*, which makes that visible.

---

## 5. Model cascades and keys

### 5.1 Three cascades, none of them live Rudi's

| Cascade | Plays | Entries, in failover order | Key (Secrets Manager) |
|---|---|---|---|
| `rudi` | Rudi, through the deployed engine | `groq-gpt-oss-120b` → `ovh-gpt-oss-120b` — live Rudi's first two models | `meetrudi/arena/rudi-groq`, `meetrudi/arena/rudi-ovh` |
| `tester` | the simulated user in intent and hybrid cases | **`tester-groq-qwen3.8-27b` (Groq free tier)** → `tester-ovh-llama-3.3-70b` → `tester-ovh-gpt-oss-20b` | **`meetrudi-groq-tester_and_judge`**, then `meetrudi/arena/sim-ovh` |
| `judge` | evaluates Rudi's replies: `expect`, `success_when`, `abort_when` | **`judge-groq-gpt-oss-120b` (Groq free tier)** → `judge-ovh-gpt-oss-120b` → `judge-ovh-llama-3.3-70b` | **`meetrudi-groq-tester_and_judge`**, then `meetrudi/arena/sim-ovh` |

The tester and the judge try the **Groq free tier first**, on the separate Groq account you set
up for them, before any OVH credit is spent. All three cascades live in `config/cascades.json` in
the arena bucket (seeded from `services/training-arena/seed/defaults/config/cascades.json`) and are
edited by a super-admin in Engine control.

### 5.2 A separate key must be a separate account

Providers limit by account, not by key. **Groq applies rate limits per organisation** (a second
project inside one organisation adds no capacity); **OVH limits per Public Cloud project and per
model** (400 requests per minute with a key). So the arena's keys come from:

| Key | Account | Status |
|---|---|---|
| `meetrudi-groq-tester_and_judge` | a separate Groq organisation for the tester and the judge | **exists** |
| `meetrudi/arena/rudi-groq` | a separate Groq organisation for the arena's Rudi | to create |
| `meetrudi/arena/rudi-ovh` | an arena OVH project for Rudi's fallback | to create |
| `meetrudi/arena/sim-ovh` | a second arena OVH project for the tester / judge fallback | to create |

Until the two `rudi-*` keys exist, Rudi's turns in the arena cannot run: the engine reports the
missing key as an engine failure and the case ends as `error`.
Until `sim-ovh` exists, the tester and judge run on the Groq free tier alone and simply wait when
its per-minute window is full (§5.5).

### 5.3 The tester and the judge

- **Tester — `qwen/qwen3.8-27b` on Groq**, the only non-gpt-oss chat model on Groq's free tier
  (checked 2026-10-14; a Preview model Groq may withdraw at short notice — the OVH fallback covers
  that). A different family from Rudi's gpt-oss, so Rudi is not talking to a copy of himself.
  Fallbacks: Llama 3.3 70B, then gpt-oss-20b, on OVH.
- **Judge — `openai/gpt-oss-120b` on Groq**, with low reasoning effort so the verdict fits the reply
  ceiling and the per-minute allowance. Fallbacks: the same model, then Llama 3.3 70B, on OVH.
  **Caveat:** the judge is Rudi's model family, and judges can favour their own family; the judge
  calibration suite (§15) decides whether Llama — or a decision model (§5.6) — judges better.
- Both share the tester-and-judge Groq account but not a model, and Groq limits per model, so they
  do not eat each other's allowance. Neither touches live Rudi's keys or the arena's Rudi keys —
  the API refuses a tester or judge entry with a Rudi key. Rudi's latency is timed on the engine
  call alone, so tester and judge speed never shows in Rudi's figures.

### 5.4 Guard rails on the Rudi cascade

`meetrudi-engine-turn` accepts an arena cascade only if every endpoint host is one live Rudi's own
`config/ai_endpoints.json` already uses (the EU/DPA-cleared providers stay listed in one place),
every key starts with `meetrudi/arena/rudi-`, and at least one entry is on. Otherwise it answers
`bad_cascade` and the case ends in `error` — it **never falls back to a live key**, which is also
why the in-code Groq fallback is dropped while an arena cascade is in force.

### 5.5 Preventive failover and mid-flight retries (tester and judge)

`services/training-arena/src/arena_llm.py`. Before every tester or judge call the client works out
what the call needs — the prompt's tokens plus the reply ceiling — and checks each entry in order:

| Source of the allowance | What it covers |
|---|---|
| Groq's response headers | `x-ratelimit-remaining-tokens` (tokens per **minute**) and `x-ratelimit-remaining-requests` (requests per **day**), each with a reset such as `2m59.56s` |
| OVH's response headers | `x-ratelimit-remaining-minute` (requests per minute) and `ratelimit-reset` |
| The entry's configured `limits` | what no header reports — Groq's requests per minute and tokens per day; counted by the arena itself |
| A 429 with `retry-after` | the entry is kept out until then |

An entry whose remaining allowance would not cover the call (with a 15% safety margin) is
**skipped before anything is sent** — so a free-tier window never runs dry part-way through a
turn, and the call moves to the next entry. The tracker's state lives in the arena bucket
(`runtime/limits/`), so parallel runners see each other's use; it is approximate by nature, which
is what the margin is for.

When a call fails anyway — 429, timeout, 5xx, network, an empty reply, a missing key — the client
moves to the next entry at once. If every entry is merely at its limit, the runner **waits for the
window** (up to 70 s; pacing, not an error). If every entry fails, the **step** is retried with
backoff (4 s, then 8 s); the case state is checkpointed after every step, so even a crashed runner
resumes at the step that was in flight and never repeats a finished one. Rudi's own turns are
retried the same way at step level; inside the engine, live Rudi's gateway fails over reactively.

### 5.6 Decision models — ready, not yet chosen

`arena_eval.py` routes every criterion to an **evaluator**. Today that is the LLM judge. The
architecture also takes **decision models** — non-generative models that answer a typed question
with a probability instead of writing a verdict:

- **System One** (TypeSafe Jev, eu/jev, Cloudflare Clef): the transcript goes in as `state`, each
  criterion as a yes/no (`noul`) question, several per request; the answer is a probability. An
  answer too close to 0.5 (`escalate_below`) is re-judged by the LLM judge — the pattern a CMU
  preprint (arXiv 2609.26550) found matched a frontier judge's accuracy at 41% of the cost.
- **Classifiers** (moderation APIs, guard models): a label and a score for one text, turned into
  met / not met by a rule.

A criterion can name its evaluator in the input file (`{text, evaluator}`), or a `routes` pattern
in `config/evaluators.json` can send matching criteria to one. A switched-off choice falls back to
the LLM judge and the verdict says so. A provider marked `eu: false` stays off until it is also
marked `signed_off` (CLAUDE.md §0.3).

**Early scan (2026-10-14), to confirm together** — all entries ship switched off:

| Candidate | What it is | Where | Free | Fit |
|---|---|---|---|---|
| **eu/jev** (`eu-jeff-latest`) | Jev-compatible decision model | Hetzner, Germany; DPA published | €5 credit; €0.037 per M input tokens after | general rubric criteria; trial first |
| TypeSafe **Jev** (`jev-1.13.0`) | the original decision model | **US** — needs sign-off | none documented | benchmark reference only |
| **Qwen3Guard-Gen-8B** | safety classifier incl. suicide & self-harm, 119 languages | OVH Gravelines (beta) | free | crisis / self-harm content; exact OVH model id to check |
| **Mistral Moderation 2** | per-category moderation scores (self-harm, health) | Mistral, EU endpoint (moderation there unverified) | priced free | self-harm and "dosing advice" signals |
| Cloudflare **Clef** | open-weight System One model | Workers AI (no EU pinning) or self-host in the EU | daily allowance | candidate for self-hosting later |

Decision models are strongest where the verdict can be read off the text, weakest where it must
be reasoned out ("reveals or paraphrases its instructions"); those stay with the LLM judge. A
classifier can say a user disclosed self-harm, but whether Rudi then named a helpline is still a
rubric question — the eu/jev path or the LLM judge.

---

## 6. Costs and budgets

### 6.1 Prices and the AWS overhead

Every cascade entry — Rudi, tester, judge, decision model — carries a price in euro per million
tokens (`in`, `out`) with a `basis`:

- `list` — the provider's published price. OVH (from `ai_endpoints.json`, 21 Sep 2026):
  gpt-oss-120b €0.08 / €0.40, gpt-oss-20b €0.04 / €0.15, Llama 3.3 70B €0.67 / €0.67. Groq
  gpt-oss-120b: USD 0.15 / 0.60 (checked 2026-10-14), entered as €0.13 / €0.52.
- `free tier` — €0. The tester-and-judge Groq account is used this way.
- `estimate` — our best figure where none is confirmed.

**AWS overhead is part of every cost and every budget.** It is an assumed unit price, not a
measurement — Lambda time for the runner and `engine-turn`, queues, S3 — set in Engine control:
**€0.00015 per Rudi turn** and **€0.0002 per case** by default. It shows as its own role (`infra`,
"AWS (estimate)") everywhere spend is shown.

### 6.2 Accounting

- **Tokens come from the provider.** Every OpenAI-compatible response reports `usage`; the
  engine-turn function returns Rudi's, and the arena's client records the tester's and the judge's.
  Cost = tokens × the price of the entry that answered.
- A rate-limited attempt is not billed. A provider that reports no usage is estimated from text
  length and marked.
- Spend is recorded per message, per judged check, per case, and rolled up by the dispatcher into
  the batch, the submitter's day, the arena's month, and per role and per model for the dashboard.

### 6.3 Budgets — four levels

| Level | Set by | Default / ceiling | Enforced by | When reached |
|---|---|---|---|---|
| Case | input file `limits.max_cost_eur` | €0.05 / €0.50 | runner, **before every model call** | case stops: `over_budget`, `case_budget` |
| Batch | input file `batch.max_cost_eur` | €10 / €100 | dispatcher, at release | no further case starts; the rest are `over_budget`, `batch_budget`; batch `budget_exhausted` |
| User, per day | super-admin | €20 | dispatcher | that user's batches wait until tomorrow |
| Arena, per month | super-admin | €150 | dispatcher | every batch waits; a super-admin can raise it |

- **Before every call**, the runner adds that call's expected cost — the prompt counted, a typical
  reply, priced at the dearest enabled entry the cascade could fail over to — and, for a Rudi turn,
  the AWS overhead of the turn, and compares with the case ceiling. The check errs high on
  purpose: a case may stop a little before its ceiling, never after it.
- **At release**, the dispatcher reserves each case's ceiling against the batch, the day and the
  month, and swaps the reservation for actual spend when the case ends. No level is ever overrun by
  more than the calls already in flight.
- A budget stop is its own outcome, never `failed`: it says nothing about Rudi.

### 6.4 Before launch

Validation estimates each case from its mode and length, priced at the first entry of each
cascade plus the AWS overhead, and shows the batch estimate next to its budget — with a warning
when a batch or a case looks set to exceed its ceiling.

### 6.5 What it costs

With the tester and judge on the Groq free tier, a 6-turn intent case costs roughly **€0.005**
(Rudi's turns plus AWS overhead); when they fail over to OVH it rises to about **€0.012**. 1,000
intent cases: about €5–12. The 7-case sample batch comes to €0.0398, of which €0.0055 is AWS
overhead.

---

## 7. Throughput control

### 7.1 Settings (super-admin, `settings/engine.json`)

| Setting | Default | Effect |
|---|---|---|
| `max_parallel_cases` | 2 | Conversations in flight across all batches. |
| `per_batch_parallel` | 2 | Slots one batch may hold, so a large batch cannot starve the rest. |
| `launch_rate_per_min` | 6 | New cases released per minute (token bucket). |
| `min_think_time_s` | 2 | Pause before each user message; spreads model calls out. |
| `live_reserve` | 6 | Lambda executions the arena never uses. |
| `breaker` | on, 10 min | Pause the arena after 3 consecutive rate-limited Rudi turns — a sign the arena's own quota is spent. |
| `quiet_hours_only` | off | Hold every batch outside 21:30–06:30 Europe/Brussels, when live traffic is lowest. |
| `max_turns_cap`, `turn_timeout_s`, `retries_per_turn` | 40, 60, 2 | Ceilings a batch file cannot exceed. |
| `daily_case_quota_per_user` | 500 | Fair use between testers. |
| `part_token_budget` | 450,000 | Where results files split. |
| budgets, cascades, prices | §5, §6 | Kept in their own files, edited from the same pane. |
| engine state | running | `running`, `paused`, `draining`, `killed` (kill switch). |

Settings are read at every tick, so a change applies from the next case released. A batch records
the settings, budgets and cascades it launched under.

### 7.2 Why the dispatcher is its own function

All pacing and budget checks live in one place, run **one tick at a time** (the FIFO queue has a
single message group), and are driven by plain files in S3. That gives parallelism, rate control
and budgets together, makes pause / drain / kill a one-field change, and makes the dispatcher the
single writer of progress, spend and the batch index — no two functions ever race to update the
same number. "Case finished" messages wake it immediately; a one-minute EventBridge tick is the
safety net.

### 7.3 The concurrency budget — the one thing still shared with live Rudi

Separate cascades remove contention for model quota. Lambda concurrency is the one shared
resource left. `aws lambda get-account-settings` on 2026-10-13: **ConcurrentExecutions = 10**, the
starter quota, shared by every function in the account — including the WhatsApp processor and the
live call relay. One arena case holds **2** executions while Rudi is thinking (the runner, and
`engine-turn`). Therefore:

`max_parallel_cases ≤ floor((account limit − live_reserve) ÷ 2)` → **2** today.

The console enforces this ceiling and shows the budget. To unlock real parallelism, raise the
quota. It is free, and it is a **console action for an account admin** — `rudi-deployer` is not
allowed to call Service Quotas (verified 2026-10-13, `AccessDeniedException`):

1. AWS Console, region **eu-central-1** → **Service Quotas** → **AWS services** → **AWS Lambda**.
2. Open **Concurrent executions** → **Request increase at account level**.
3. Enter **1000** → **Request**. AWS usually answers within a day or two.

Independent of the arena, a limit of 10 is thin for live traffic alone.

---

## 8. Life of a batch, and how results are cut

1. **Submit.** The browser uploads to `inbox/` through a signed URL, or the tester names an object
   already in `inbox/`. The API creates the batch (`TA-<yyyymmdd>-<XXXX>`), copies the file to
   `batches/<id>/input/` unchanged, and starts ingest in validate mode.
2. **Validate.** Ingest streams the file one case at a time, applies the rules in
   [formats §3.5](training-arena-formats.md), estimates the cost (§6.4) and writes
   `validation.json`. Errors → `invalid`.
3. **Launch.** On the tester's click, ingest writes one `spec.json` per case, the batch becomes
   `queued`, and a tick is sent to the dispatcher.
4. **Dispatch.** Each tick, the dispatcher works out free slots, launch allowance and budget room
   (§6.3, §7), picks the next cases round-robin across active batches, writes a lease per case,
   and puts one message per case on the cases queue.
5. **Run.** A runner takes one message and runs that case to its end (§9). It then **renders the
   case's results document** — the exact text that will appear in the output — stores it as
   `cases/<nnnn>/result.yaml` with its estimated token size, and reports "case finished" (outcome,
   size, spend) to the dispatcher queue. The runner never holds more than this one conversation.
6. **Count.** The dispatcher — the single writer — releases the lease and reservation, adds the
   case's line to the **batch index** (blocks of 1,000 cases), updates the batch's progress and
   spend and the day's stats, and releases the next case. This is the "update after each case" the
   dashboard shows; there is no live stream.
7. **Plan.** When a batch has no case left, the assembler reads the batch index — sizes only, no
   transcript — and plans the parts: whole cases in input order, each part closed before it would
   pass the token budget. **The number of parts is now final**, before a byte is written. The
   plan is stored as `output/plan.json`.
8. **Stream.** Each part is written by reading its case documents from S3 one by one and appending
   them to a multipart upload. Memory is one case plus one 8 MB chunk, whatever the batch size.
   Parts are independent, so a large batch writes them in parallel invocations, and a failed part
   is simply redone. The index CSV is written the same way. The batch becomes `completed`,
   `completed_with_errors`, `budget_exhausted` or `cancelled`.

So the "1 of 3" needs neither a growing interim file nor results held in a running computer: the
per-case documents are the durable records (the console reads them too), and the parts are a
planned concatenation of them. A **snapshot** of a running batch is built the same way from the
cases finished so far and carries its time in the name, so its own "of N" never collides with the
final one.

Batch states: `draft → validating → invalid | ready → queued → running ⇄ paused → assembling →
completed | completed_with_errors | budget_exhausted | cancelled`.

---

## 9. The case runner

One invocation, one case, one isolated conversation.

```
load spec → engine-turn(open)            # Rudi's greeting, no model call
repeat:
    budget check           ← spend + expected cost of the next call ≤ case ceiling?
    next user message      ← script entry, or the tester cascade (persona + objective + transcript)
    wait think-time
    engine-turn(turn)      → Rudi's reply, state, usage   (arena Rudi cascade; retry on rate-limit / timeout)
    record both messages: timestamps, latency, model, tokens, cost, phase, signals, guard events
    judge                  ← judge cascade, for expect / success_when / abort_when
    checkpoint to S3
until a stop condition holds
render result.yaml → tell the dispatcher (outcome, size, spend)
```

- **Scripted messages** are sent verbatim, in order. A `gap` advances the simulated clock passed
  to the engine; the arena does not actually wait.
- **The tester** receives the persona, the objective, the `reveal` facts and the transcript, and
  returns `{message, pushing, note}`. `pushing: true` marks an attempt. Its instructions forbid
  inventing real-looking identifiers; its output still passes the engine's de-identification like
  any user message.
- **The judge** returns `{met, why}` per criterion. It sees the criterion and the transcript, never
  the tester's objective, so it cannot be argued into agreeing. Deterministic checks
  (`expect_phase`) need no model and cost nothing.
- **Stop conditions**, in order: cancel flag → case budget → `abort_when` met → `success_when` met
  → Rudi concluded the session → `max_attempts` → `max_turns` / script end → unrecoverable error.
- **Retries:** a rate-limited or timed-out turn is retried with backoff (`retries_per_turn`,
  default 2: after 4 s, then 8 s). The user message is not re-generated, so a retry cannot change
  the conversation. Exhausted → the case ends as `error`, with every attempt logged.
- **Long cases:** state is checkpointed after every turn. With less than 90 s of its 15 minutes
  left, the runner re-queues the case with a pointer to the checkpoint and exits.
- **Lost runners:** a case whose lease goes 20 minutes without a heartbeat is re-queued once from
  its checkpoint, then recorded as `error: runner_lost`.
- **Logging:** opaque IDs, event types, timings and costs only — never message text (CLAUDE.md §5).

### Error vocabulary

| Code | Meaning | Retried |
|---|---|---|
| `rate_limited` | Every entry of the arena's Rudi cascade returned 429 | yes |
| `timeout` | No Rudi reply within `turn_timeout_s` | yes |
| `engine_failed` | The engine raised for another reason | no |
| `bad_cascade` | The engine refused the arena's Rudi cascade (§5.4) — a configuration error | no |
| `tester_failed` | The tester or judge cascade failed after its own retries | no |
| `runner_lost` | The runner died and the re-queue also failed | once |

---

## 10. Storage — `s3://meetrudi-arena-data-<account-id>/`

```
inbox/<username>/…                         files waiting to be submitted (30 days)
batches/<batch-id>/
    manifest.json                          status, counts, spend, settings / budget / cascade snapshot
    input/<batch-id>[__name]__input.yaml   as submitted, unchanged
    validation.json                        incl. the cost estimate
    cases/<nnnn>/spec.json                 one case, defaults resolved
    cases/<nnnn>/checkpoint.json           in-flight state (removed on finish)
    cases/<nnnn>/result.yaml               the case's results document, rendered once
    index/<block>.json                     per case: size, outcome, spend — 1,000 cases a block
    output/plan.json                       the part plan
    output/<batch-id>[__name]__results__pNNofMM.yaml
    output/<batch-id>[__name]__index.csv
users/<username>.json                      role, password hash, status
sessions/<sha256(token)>.json              live sessions
settings/engine.json · settings/budgets.json
config/cascades.json                       rudi / tester / judge entries, with prices
prompts/arena_tester_prompt.md · arena_judge_prompt.md
runtime/leases/… · runtime/dispatcher.json in-flight accounting and reservations (2 days)
stats/daily/<yyyy-mm-dd>.json              dashboard aggregates: outcomes, latency histogram, spend
audit/<yyyy>/<mm>/<dd>/…                   who did what
```

Nothing of the arena is ever written to `meetrudi-ai-data-…`, and no live conversation is ever
written here. Encryption and public-access block as on the base bucket; lifecycle rules do the
expiries.

---

## 11. Accounts and security

- **Roles:** `tester` and `superadmin`. The super-admin panes (engine control, users, audit and
  storage) are not drawn for testers, but that is cosmetic — the static bundle is public. What
  counts: **every `/admin/*` route re-checks the role server-side**, as the tester console does.
- **Passwords:** PBKDF2-HMAC-SHA256, 600k rounds, per-user salt — the tester console's scheme.
- **Create / reset:** the API generates a one-time password, shows it once, stores only its hash,
  and marks the account `must_change`. A reset also ends that user's sessions.
- **Delete:** removes the account; its batches stay, credited to "`name` (deleted)". The last
  super-admin can be neither deleted, demoted nor locked.
- **Sessions:** opaque token, stored as a SHA-256 digest, 30-minute idle expiry checked on every
  request. **Lockout** after 10 failed sign-ins; no user enumeration.
- **Bootstrap:** the first super-admin comes from the secret `meetrudi/arena/bootstrap`; the API
  fails closed (503) until it exists.
- **CORS** pinned to the Pages origin on the Function URL and on the bucket (for signed uploads).
- **Audit:** sign-ins, batch actions, user changes, settings, budget, cascade and price changes,
  breaker events.

---

## 12. Console ↔ API

| Pane | Cards | Routes |
|---|---|---|
| Dashboard | KPIs · running and queued batches (with spend) · outcomes per day · Rudi latency · errors · Rudi model mix · cases that keep failing · **costs: month budget, spend per day by role, cost per case by mode, spend by model, most expensive cases** · recent batches | `GET /dashboard?days=` |
| New batch | Choose the input · name the batch · check the file (incl. cost estimate and budget) · launch | `POST /uploads`, `POST /batches`, `POST /batches/{id}/validate`, `POST /batches/{id}/launch` |
| Batches & results | List · progress and spend · files · cases (with cost) | `GET /batches`, `GET /batches/{id}`, `GET /batches/{id}/cases`, `POST …/{pause,resume,cancel,rerun}`, `GET …/files/{name}` (signed link) |
| Case viewer | Conversation (cost per message) · result and cost · checks · errors · actions | `GET /cases/{id}` |
| Formats & templates | Downloads · naming · modes · stop criteria · budgets · S3 submission | static |
| My account | Profile, own spend today · change password | `GET /me`, `POST /account/password`, `POST /login`, `POST /logout` |
| 🔒 Engine control | Global controls · throughput · concurrency budget · protect live Rudi · **budgets** · limits · **model cascades and prices** | `GET/PUT /admin/settings`, `GET/PUT /admin/budgets`, `GET/PUT /admin/cascades`, `POST /admin/engine/{pause,resume,drain,kill}` |
| 🔒 Users | Roster (with spend) · create · reset · lock · role · delete | `GET/POST /admin/users`, `POST /admin/users/{name}/{reset,lock,unlock,role}`, `DELETE /admin/users/{name}` |
| 🔒 Audit & storage | Audit log · storage · what is being tested | `GET /admin/audit`, `GET /admin/storage`, `GET /admin/versions` |

The prototype marks each of these calls in `site/training-arena/arena.js` where the demo data
stands in for it.

---

## 13. The cost section of the dashboard

Fed by the dispatcher's daily stats, so it moves each time a case finishes; filtered by the same
time range as the rest of the dashboard.

| Card | Shows | Why |
|---|---|---|
| Budget this month | Arena spend against the monthly budget; today's spend; per-user spend today (all users for a super-admin, own for a tester) | Is there room for the batch you are about to launch? |
| Spend per day | Stacked by role — Rudi, tester, judge, AWS (estimate) — with a table view | What drives cost, and whether a change moved it. |
| Cost per case | Mean, p95 and maximum per mode (scripted / intent / hybrid), number of cases stopped by a budget | What a case typically costs; how often ceilings bite. |
| Spend by model | Euro per cascade entry, plus the AWS overhead | Where the money goes; the effect of a price or cascade change. |
| Most expensive cases | The top cases of the period: key, turns, cost against ceiling, how they stopped | The back-and-forth cases a budget is there to catch, and their keys to fix. |

Running batches show spend against their budget beside the progress bar; batch detail and case
viewer show cost by role, per case and per message.

---

## 14. IAM and deployment

Exact steps: [infra/iam/README.md](../infra/iam/README.md) §7 (Lambda quota) and §8 (role,
pass-role, secrets). In short:

1. **Outside AWS:** a Groq organisation and an OVH project for the arena's Rudi (`rudi-groq`,
   `rudi-ovh`), and a second OVH project for the tester / judge fallback (`sim-ovh`). The
   tester-and-judge Groq account (`meetrudi-groq-tester_and_judge`) already exists.
2. **IAM console:** create role `meetrudi-arena-runner` from
   `infra/iam/meetrudi-arena-runner.trust.json` + `.permissions.json`; add
   `rudi-deployer.passrole-arena-add.json` to the deployer. The role reaches its own bucket,
   queues and functions, `meetrudi-engine-turn`, and only the arena's secrets — **no live data
   bucket, no Rudi keys**.
3. **Secrets Manager:** `meetrudi/arena/bootstrap` (first super-admin), `meetrudi/arena/rudi-groq`,
   `meetrudi/arena/rudi-ovh`, `meetrudi/arena/sim-ovh`. `meetrudi-engine-turn` reads the two
   `rudi-*` keys through the existing `meetrudi-lambda-runner` grant — no change to that role.
4. **Deploy, in this order** (Windows `cmd`, from the repository root):

```cmd
python deploy.py whatsapp
python deploy.py training-arena
```

`whatsapp` adds `meetrudi-engine-turn` and redeploys the live WhatsApp functions with the shared
`webchat.chat_turn` (behaviour unchanged; 435 tests). `training-arena` builds the PyYAML layer,
creates the bucket, queues and five functions, seeds `defaults/` and `prompts/` into the arena
bucket, and prints the API URL. Put that URL into `site/training-arena/config.js` as `API_BASE`
and push `site/**` to `main`; GitHub Pages then serves
`https://meet-rudi.github.io/start-up-app/training-arena/enter_arena.html`.

One new library: **PyYAML** (in a Lambda layer built by `services/training-arena/build_layer.py`).
It is a parser, not a service — no data leaves the account, no DPA implication.

---

## 15. Tests and evals

- **Unit** (no network, synthetic data): format validation; ID and file-name generation; the part
  planner (never cuts a case, an oversize case gets its own part, parts stitch back to the same
  cases, the plan is final before writing) and the streaming writer; dispatcher fairness, rate
  limiting, pause / drain / kill, lease expiry; every stop condition of the runner against a fake
  engine and fake LLMs.
- **Costs:** price arithmetic; that case, batch, day and month budgets are never overrun beyond
  the calls in flight; reservations released on every ending, including `runner_lost`; estimated
  costs marked as such.
- **Contract and guard:** `engine_turn` against `tester_api`'s chat for the same state and input;
  with an arena cascade in force, no call uses a live secret or the in-code fallback; a cascade
  with an unknown host or a non-arena secret is refused.
- **Judge calibration** (`evals/arena-judge/`): hand-labelled transcripts the judge must agree
  with. An arena verdict is only as good as its judge, so the judge prompt and model do not
  change without this suite — the same rule §8 of CLAUDE.md applies to guardrails.
- **Smoke:** the 7-case sample batch, run end to end after every deploy.

As built: `python -m unittest discover -s services/training-arena/tests -v` (79 tests: formats,
results and planning, model client and preventive failover, evaluators incl. System One and
escalation, the case runner, dispatcher, ingest, assembler, API, and the whole pipeline end to end
in memory) and `python -m unittest discover -s services/whatsapp/tests -v` (435 tests, incl.
`test_engine_turn.py`). The judge calibration set is still to be written.

---

## 16. Build status

| Block | Scope | Status |
|---|---|---|
| A0 | Formats: parser, validator, IDs and naming, part planner, streaming writer | ✅ built, tested |
| A1 | `webchat.chat_turn()`, `gateway.using(cascade)` + usage, `meetrudi-engine-turn` | ✅ built, tested |
| A2 | Arena stack: bucket, queues, layer, functions, `deploy.py training-arena`, IAM files | ✅ deployed 2026-10-14; `/health`, CORS and the dispatcher tick checked |
| A3 | Accounts: sign-in, sessions, users, audit | ✅ built, tested |
| A4 | Ingest + runner: scripted, intent, hybrid; pricing, AWS overhead, case budget; step retries and resume | ✅ built, tested |
| A5 | Tester and judge cascades with preventive failover; evaluator layer incl. decision models | ✅ built, tested against fakes; judge calibration set to write |
| A6 | Dispatcher: parallelism, rate, fairness, breaker, kill switch, batch / user / month budgets | ✅ built, tested |
| A7 | Assembler: plan, stream, index CSV, snapshots, downloads | ✅ built, tested |
| A8 | Console wired to the API; dashboard stats and cost section | ✅ built; live mode tested against a fake API |

Proven in AWS on 2026-10-14: the crisis batch end to end (`TA-20261014-KXSA`, 4 cases, €0.0136,
4 min) — real Groq and OVH calls, Rudi failing over Groq → OVH, the free-tier pacing waiting
instead of failing, the LLM judge, results parts and the index CSV. Found on the way: the Groq
tester model (Qwen3) had its thinking eat the token budget and Groq rejected the cut-off JSON
(`json_validate_failed`); its reasoning is now off, such a rejection is asked again without JSON
mode, and provider errors keep their code. Not yet proven: the console against the real API.

---

## 17. Later versions

- **v2 — call.** The call service gets its own turn function around `brain.py`; the arena drives
  it turn by turn in text, with the call goals as case parameters. Speech (TTS/ASR) and telephony
  can be added later, priced per minute (§6.1), without changing the format.
- **v3 — WhatsApp.** The processor path — objection gate, window state, check-in scheduling — run
  with a capturing `WhatsAppProvider` implementation in place of Twilio, so nothing is sent and
  nothing is skipped. The simulated clock lets a case cross the 24-hour window and trigger the
  re-engagement runner.
- **Engine versions.** `rudi.engine: <release>` runs a pinned release or prompt set beside
  `live`; the results already record which release answered.
- **Comparison.** Two batches from the same input, side by side — outcomes, latency and cost — the
  natural gate for a prompt, model or cascade change.

---

## 18. Alternatives considered

| Alternative | Why not |
|---|---|
| Copy the engine modules into the arena stack | A second copy of Rudi can drift from the live one — the opposite of requirement 6. |
| Call the tester console's API for each turn | It would write arena conversations into the tester store and need a fake tester account. |
| Run arena turns on live Rudi's keys | Arena load would take quota and latency from live users and calls (settled: D4). |
| A second key in the live Groq organisation | Groq limits per organisation, so it would share live Rudi's limits anyway. |
| Write results parts as cases finish ("rolling" parts) | "Of N" is unknown until the end, so every part would need renaming — S3 has no rename, only copy and delete. Planning after the end is simpler and just as light. |
| Build each part in memory | Fails on a large batch; streaming keeps memory flat. |
| SQS event source with `MaximumConcurrency` as the throttle | No rate or budget control, a minimum of 2, and changing it at runtime needs permission to edit Lambda configuration. |
| Step Functions Distributed Map | Concurrency is fixed when an execution starts, it needs another role, and pause / drain / budgets would be bolted on. |
| DynamoDB for counters and leases | Would work, but every other meetrudi store is S3; a single-writer dispatcher removes the need. |
| A prefix in the live data bucket | Weaker than a bucket boundary: one mistaken policy line and arena code can write live data. |
