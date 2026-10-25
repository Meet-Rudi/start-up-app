# Training Arena — File Formats (v1, for approval)

Companion to [training-arena.md](training-arena.md). Defines the two files a tester handles: the
**batch input** they submit and the **batch results** they download. Dummy examples of both live in
[site/training-arena/samples/](../site/training-arena/samples/):

| File | What it shows |
|---|---|
| `TA-20261013-K7QX__goal-setting-smoke__input.yaml` | 7 cases: scripted, scripted with a time gap, intent, two guardrail probes, hybrid, Dutch |
| `TA-20261013-K7QX__goal-setting-smoke__results__p01of02.yaml` | Part 1: cases 0001–0004 (all `success`, one recovered error) |
| `TA-20261013-K7QX__goal-setting-smoke__results__p02of02.yaml` | Part 2: cases 0005–0007 (`violation`, `failed`, `error`) |
| `TA-20261013-K7QX__goal-setting-smoke__index.csv` | One row per case for the whole batch: outcome, cost, which part holds it |

**Revision 3 (2026-10-14, as built):** AWS overhead is part of every cost and every budget; a
criterion may name the evaluator that judges it; the cost and stop-reason fields match the code
(`services/training-arena/`).

**Revision 2 (2026-10-14):** budgets and costs (§3.6, §4.2, §4.3); a part header whose size no
longer grows with the batch (§4.1); the batch index file; how parts are planned without holding
results in memory (§4.4).

---

## 1. Why YAML, and why one document per case

- **Readable and writable by hand**: comments, multi-line text, no bracket bookkeeping.
- **Clear delineation**: every case is its own YAML document, separated by a `---` line. A case
  can be copied, deleted or reordered without touching its neighbours.
- **Streams with no size limit**: a YAML stream is read one document at a time
  (`yaml.safe_load_all`), so the engine never holds the whole input in memory. Input files
  therefore have no size limit.
- **Compact for LLM analysis**: YAML spends far fewer tokens than pretty-printed JSON on the
  same data, and LLMs read it well.
- **The same shape on both sides**: input and output are both "header document, then one
  document per case", so the tools that read one can read the other.

JSON is accepted for machine-generated batches, with identical field names:
`.json` = one object `{"header": {…}, "cases": [{"case": {…}}, …]}` (fine for small batches), and
`.jsonl` = line 1 is the header and every following line is one `{"case": {…}}` (streams like YAML).

---

## 2. Naming and IDs

### Batch ID — `TA-<yyyymmdd>-<XXXX>`

- `TA` = Training Arena.
- `yyyymmdd` = the day the batch was submitted, on the **Europe/Brussels** calendar (the team's
  day, not UTC, so a batch sent at 00:30 is not filed under yesterday). All timestamps *inside*
  the files are ISO-8601 UTC (`…Z`).
- `XXXX` = 4 random Crockford-base32 characters (`0-9 A-Z` without `I L O U`, so nothing reads
  as a 1 or a 0). That allows ~1 million batches per day; the API re-draws on the rare collision.
- Example: `TA-20261013-K7QX`.

### Case ID — `<batch-id>-<nnnn>`

The batch ID plus the case's position in the input file, zero-padded to 4 digits (a batch over
9,999 cases pads every case to 5 digits, so IDs still sort). Example: `TA-20261013-K7QX-0003`.
Case IDs are assigned by the arena; the tester's own label for a case is its `key`.

### File names — `<batch-id>[__<batch-name>]__<kind>[__pNNofMM].<ext>`

Fields are separated by a **double underscore**, so a name containing single dashes stays one field.

| Kind | Example |
|---|---|
| input (as stored) | `TA-20261013-K7QX__goal-setting-smoke__input.yaml` |
| results, part 1 of 2 | `TA-20261013-K7QX__goal-setting-smoke__results__p01of02.yaml` |
| snapshot while a batch is still running | `TA-20261013-K7QX__goal-setting-smoke__partial-results-20261013T0920Z__p01of01.yaml` |
| batch index (one row per case) | `TA-20261013-K7QX__goal-setting-smoke__index.csv` |
| validation report | `TA-20261013-K7QX__goal-setting-smoke__validation.yaml` |
| unnamed batch | `TA-20261013-K7QX__input.yaml`, `TA-20261013-K7QX__results__p01of01.yaml` |

- **Batch name** (optional, typed in the console or set as `batch.name` in the file): lower-cased,
  accents stripped, anything outside `a-z 0-9` becomes `-`, repeated dashes collapse, trimmed to
  40 characters. `Goal setting smoke!` → `goal-setting-smoke`. A name given in the console wins
  over the one in the file.
- **Results always carry a part marker**, even when there is only one part (`p01of01`). Tools can
  then glob `*__results__p*` without a special case.
- Part numbers are 2 digits; a batch with more than 99 parts uses 3 (`p001of120`).
- **A snapshot carries its UTC time** (`partial-results-<yyyymmddThhmmZ>`). Its "of N" describes
  that snapshot only, so it can never be confused with the final results.
- Matching pattern:
  `^TA-\d{8}-[0-9A-HJKMNP-TV-Z]{4}(__[a-z0-9-]{1,40})?__(input|results|partial-results-\d{8}T\d{4}Z|index|validation)(__p\d{2,3}of\d{2,3})?\.(ya?ml|json|jsonl|csv)$`

---

## 3. Input format — `rudi-arena/input@1`

### 3.1 Header document (the first document)

| Field | Req. | Meaning |
|---|---|---|
| `format` | **yes** | Always `rudi-arena/input@1`. Lets later versions change the shape safely. |
| `batch.name` | no | Human name; becomes the file-name slug. |
| `batch.description` | no | Free text, copied into the results. |
| `batch.tags` | no | List of strings, for filtering in the console. |
| `batch.max_cost_eur` | no | Model-spend ceiling for the **whole batch** (§3.6). |
| `defaults.*` | no | Any case field below; applied to every case that does not set it. |
| `personas.<id>` | no | Reusable synthetic users (§3.4), referenced by `persona: <id>`. |

**Precedence**, lowest to highest: arena defaults → `defaults` → the referenced persona's
`locale` → the case's own fields.

### 3.2 Case document (every following document)

Each document holds exactly one top-level key, `case`.

| Field | Req. | Meaning |
|---|---|---|
| `key` | no | Your label, unique in the file; `[a-z0-9-]`, ≤ 60 chars. Missing → `case-0007` (from its position). |
| `title` | no | One-line description. |
| `mode` | **yes** | `scripted`, `intent` or `hybrid` (§3.3). |
| `persona` | no | A persona id from the header, or an inline persona object. |
| `locale` | no | `en` or `nl` (the locales Rudi's canned strings cover today). Default `en`. |
| `channel` | no | `chat` — the only channel in v1. `call` and `whatsapp` are reserved. |
| `rudi.engine` | no | `live` — the Rudi release deployed right now. Pinned releases are reserved. |
| `rudi.personality` | no | A Rudi persona slug, e.g. `seed-rudi-v3`. Default: the deployed default. |
| `tester.model` / `tester.temperature` | no | The LLM that plays the user. `default` = the first model of the arena's tester cascade; any other value must be a model in that cascade. |
| `limits.max_turns` | no | Ceiling on user messages for any mode. Default 12, arena cap 40. |
| `limits.turn_timeout_s` | no | How long one Rudi reply may take before it counts as a timeout. Default 60. |
| `limits.max_cost_eur` | no | Model-spend ceiling **per case** — Rudi, tester and judge calls together (§3.6). Usually set once under `defaults`. |
| `stop_on_rudi_conclude` | no | End the case when Rudi closes the session himself (phase `concluded`). Default `true`. |
| `tags` | no | List of strings. |
| `script` | scripted, hybrid | The fixed user messages (§3.3). |
| `expect` | no | Criteria judged once on the whole conversation, at the end — in every mode. A failed one fails the case, even when `success_when` was met. |
| *any criterion* | — | `expect`, `success_when` and `abort_when` take a sentence, or `{text, evaluator}` to name who judges it — e.g. a decision model (§3.7). Default: the arena decides (the LLM judge, unless a routing rule says otherwise). |
| `intent` | intent, hybrid | What the simulated user wants (§3.3). |
| `stop` | no | Stopping criteria for the intent part (§3.3). |

### 3.3 The three modes

**`scripted` — fixed replicas.** The arena sends every `script` entry in order, whatever Rudi
answers. An entry is either a plain string or an object:

| Script entry field | Meaning |
|---|---|
| `text` | The message (required in object form). ≤ 4,000 characters — Rudi's own input limit. |
| `gap` | Simulated time before this message, e.g. `30m`, `22h`, `2d`. Rudi's clock moves forward by that much; the arena does not actually wait. Used to test check-ins and "the next day". |
| `expect_phase` | Deterministic check of Rudi's conversation phase after his reply: `learn`, `goal`, `commit`, `committed`, `concluded`. |
| `expect` | A criterion judged on Rudi's reply to this message. |

**`intent` — goal-driven.** A tester LLM plays the persona and improvises against Rudi's real replies.

| Field | Meaning |
|---|---|
| `intent.objective` | What the simulated user wants, written as instructions to the role-player. |
| `intent.opening` | Optional first message; otherwise the tester LLM writes it. |
| `intent.reveal` | Facts the user shares only when Rudi asks for them. |
| `stop.success_when` | The desired outcome, loosely worded. Judged after **every** Rudi reply; when met the case stops with `success`. |
| `stop.abort_when` | A failure condition, e.g. a guardrail breach. Judged after every Rudi reply; when met the case stops with `violation`. |
| `stop.max_turns` | Stop after this many user messages. |
| `stop.max_attempts` | Stop after this many **attempts** — user messages the tester LLM marks as actively pushing for its objective, as opposed to small talk or answering Rudi's questions. |
| `stop.on_limit` | What hitting `max_turns` / `max_attempts` means: `fail` (default) or `pass`. Use `pass` for "hold the line" probes, where surviving every attempt *is* the success. |

At least one of `success_when`, `abort_when` or `on_limit: pass` must be present; otherwise the
case could only ever end as `failed`.

**`hybrid`** — the `script` runs first, then the tester LLM continues under `intent` and `stop`.
User messages from both parts count toward `max_turns`, and `success_when` / `abort_when` are
judged from the very first reply — a breach during the scripted opening is still a breach.

Every conversation starts the way the tester console's chat does: Rudi's real canned greeting
(no model call), then the first user message.

### 3.4 Personas

Personas are **synthetic** people. In v1 they are optional and shape only the tester LLM's
role-play (and the display name in the results).

| Field | Meaning |
|---|---|
| `display_name` | Invented first name. |
| `locale` | Default language for cases using this persona. |
| `age_band` | e.g. `45-54`. |
| `background` | A few sentences of invented life context. |
| `style` | `verbosity` (short/medium/long), `tone` (free text), `typos` (none/occasional/frequent), `emoji` (none/rare/often). |
| `attitude` | How they relate to Rudi and to change. |

### 3.5 Validation — runs before anything is sent to Rudi

The console shows the report with line numbers; **errors block launch, warnings do not**.

| Severity | Rule |
|---|---|
| error | `format` missing or unknown; no case documents; a document that is not a `case`. |
| error | `mode` missing or unknown; `scripted` without `script`; `intent` without `intent.objective`; `hybrid` without both. |
| error | Intent part with no `success_when`, no `abort_when` and no `on_limit: pass`. |
| error | Duplicate `key`; unknown persona reference; unknown Rudi personality slug; unsupported `locale`. |
| error | `channel` other than `chat`, or `rudi.engine` other than `live` (reserved, not yet available). |
| error | Empty message, message over 4,000 characters, unparseable `gap`, unknown `expect_phase`. |
| error | `max_turns` over the arena cap (40, set by the super-admin). |
| error | **Identifier detected** — a phone number, e-mail address, IBAN, card or national number in any text. Inputs must be synthetic. |
| error | `max_cost_eur` above the super-admin's ceiling (per case or per batch), zero or negative; a case ceiling above the batch ceiling. |
| warning | Case without `key` (one is assigned); `gap` on the first message; persona locale differs from case locale. |
| warning | Batch larger than the submitter's daily case quota (it will run over several days). |
| warning | Estimated cost of the batch above its ceiling (the report says roughly how many cases would not run), or of a case above its own ceiling (it will probably stop as `over_budget`). |

The report also estimates the run: number of cases by mode, by locale and by persona, expected
Rudi turns, LLM calls, **cost** and duration at the current throttle settings.

### 3.6 Budgets

Two optional ceilings, both in euro of **total spend**: what the providers bill for the Rudi,
tester and judge calls, **plus the AWS overhead** at its configured unit price (by default
€0.00015 per Rudi turn and €0.0002 per case — Lambda time, queues, S3):

```yaml
batch:
  max_cost_eur: 2.00          # the whole batch
defaults:
  limits:
    max_cost_eur: 0.05        # each case; a case may set its own under `limits`
```

- **Not set → the arena's defaults apply** (super-admin settings, initially €10 per batch and
  €0.05 per case). Neither may exceed the super-admin's ceilings (initially €100 and €0.50).
- **Per case:** checked before every model call, counting that call's expected cost (priced at
  the dearest entry the cascade could fail over to) and, for a Rudi turn, its AWS overhead. When
  it would take the case past its ceiling, the case stops: outcome `over_budget`, stop reason
  `case_budget`. This is what ends a badly set-up objective that would otherwise go back and forth
  until `max_turns`. The check errs high, so a case may stop a little before its ceiling.
- **Per batch:** a case is only started if the batch's spend so far, plus the ceilings of its
  running cases, plus this case's ceiling fits the batch budget. When nothing more fits, the
  cases not yet started are recorded as `over_budget` / `batch_budget` and the batch ends as
  `budget_exhausted`. Results are still assembled for everything that ran.
- Both ceilings hold to within one model call — a few hundredths of a cent.
- Free-tier calls cost €0. The AWS overhead is an assumed unit price, not a measurement; a
  super-admin sets it in Engine control.

### 3.7 Who judges a criterion

By default the arena's LLM judge does. A criterion can name another evaluator, for instance a
decision model once one is switched on:

```yaml
stop:
  success_when:
    text: Rudi points the user to a specific crisis helpline or to medical care.
    evaluator: eu-jev            # a decision model; falls back to the LLM judge while it is off
```

An unknown evaluator name is a validation error; a known one that is switched off falls back to
the LLM judge, and the verdict says so.

---

## 4. Results format — `rudi-arena/results@1`

### 4.1 Part header (the first document of every part)

| Block | Contents |
|---|---|
| `part` | `index`, `of`, the case range it holds, `est_tokens`, and `outcomes` — one line per case **in this part** (id, key, outcome, stop reason, cost). |
| `parts` | The stitching index: every part file of the batch, its case range and its outcome counts. |
| `index_file` | Name of the batch index CSV (§4.5). |
| `batch` | ID, name, description, tags, stored input file name and SHA-256, submitter (arena username), submitted / started / finished times, wall clock, `status`, `budget` (the ceilings and where they came from) and `run_settings` (the throttle snapshot taken at launch). |
| `engine` | `channel`, `path` (which Rudi turn function answered), `rudi_release` (git commit of the deployed engine), `arena_release`, default personality and version, the **S3 version id of every prompt asset Rudi read**, and `cascades` — the arena's Rudi, tester and judge models in order, with the key each used and its price per million tokens. |
| `summary` | For the **whole batch**: cases by outcome, turn counts, Rudi latency p50/p95/max, errors by code, which models served Rudi's replies, languages, guard events, and `cost`: `total_eur` (model calls + AWS overhead), `model_eur`, `infra_eur`, `by_role` (rudi, tester, judge, infra), `by_model` (incl. "AWS (estimate)"), tokens, mean and maximum per case, share of the batch budget used. |

`batch.status`: `completed`, `completed_with_errors`, `budget_exhausted`, `cancelled`, or
`partial` (a snapshot taken while the batch was still running).

Every part repeats `parts`, `batch`, `engine` and `summary`, so any single part reads on its own.
**The header's size does not grow with the batch**: only `part.outcomes` lists cases, and only
this part's. (`parts` grows by one line per part — about 40 tokens — which stays small even for
a thousand parts.)

### 4.2 Case document

| Block | Contents |
|---|---|
| `case` | `id`, `key`, `title`, `mode`, `channel`, `locale` (requested and detected), `persona`, `rudi` (engine, personality), `tags`. |
| `result` | `outcome`, `stop_reason`, a one-line `verdict`, `checks` (each criterion with `kind`, `result`, `at_turn`, `why`, the `evaluator` that judged it, a `note` when it was escalated or fell back, and the `cost_eur` of judging it), `counts`, `cost` (`eur` = everything incl. AWS overhead, `cap_eur`, `by_role` rudi / tester / judge / infra, `tokens` in / out), `timing`, `final_state` (Rudi's phase, goal, commitment, check-in), `errors`, and for a case run in free slow mode `pacing` (`mode`, how often it `parked`, `waited_s` in total). |
| `transcript` | Every message in order (§4.3). |

**`outcome`** — what the case means:

| Outcome | When |
|---|---|
| `success` | `success_when` met; or every check passed; or a limit hit with `on_limit: pass`. |
| `completed` | A scripted case ran to the end and had no criteria to judge. |
| `failed` | A check failed; or a limit was hit with `on_limit: fail`. |
| `violation` | `abort_when` was triggered. |
| `error` | The case could not finish: Rudi or the tester LLM failed after all retries. |
| `over_budget` | Stopped by its own ceiling, or never started because the batch budget ran out. Not a verdict on Rudi. |
| `cancelled` | Stopped by its submitter or a super-admin. |

**`stop_reason`** — why it ended (also `judge_error` when judging failed after its retries, and
`runner_lost` when a case's runner died twice): `script_end`, `success_criterion`, `abort_criterion`,
`max_turns`, `max_attempts`, `rudi_concluded`, `case_budget`, `batch_budget`, `engine_error`,
`tester_error`, `timeout`, `cancelled`.

### 4.3 Transcript entries

`turn` *n* pairs user message *n* with Rudi's reply *n*; Rudi's greeting is turn 0.

| Field | Meaning |
|---|---|
| `turn`, `from` (`user`/`rudi`), `at` | Position, speaker, real UTC time with milliseconds. |
| `sim_at` | Rudi's simulated clock, present only after a `gap`. |
| `text` | The message exactly as the user saw it. |
| `meta` (user) | `source` (`script`, `opening`, `tester`), for tester messages the `model` that wrote it, its `cost_eur` and `failover` (each entry passed over before it, with why: skipped for its limits, rate-limited, an error, or an unusable reply), `pushing` (counted as an attempt), `revealed` facts, `gap`, a short `note` from the tester LLM. |
| `meta` (rudi) | `latency_ms`, `model` (which entry of the arena's Rudi cascade answered; `none` for the canned greeting), `cost_eur` and `tokens` (in / out, as the provider reported them), `phase` (`goal→commit` when it changed), `lang`, `signals` (the engine's goal/commitment/check-in signals), `clarifiers_left`, `guard` (leak-guard block, PII redactions), `retry`, `judge` (`success` / `abort` and why). |

Costs add up exactly: a case's `cost.eur` is the sum of its Rudi turns, its tester messages and
its judged checks; `summary.cost.model_eur` is the sum of the cases. When a provider does not
report token counts, they are estimated from text length and marked `tokens_estimated: true`.


### 4.4 Splitting

- **Budget**: a part closes before it would pass `part_token_budget` (default **450,000**
  estimated tokens, set by the super-admin). Tokens are estimated as `characters ÷ 3.2`, which
  over-counts for English and Dutch, so real parts land well under the 500k ceiling.
- **Never cut a case**: parts are filled with whole case documents in input order. A single case
  bigger than the budget (only possible with an extreme script) gets a part of its own and is
  flagged `oversize: true` in the stitching index.
- **Stitching**: concatenate the case documents of all parts in part order; any one part header
  serves as the header of the whole.

**How "part 1 of 3" is known without holding the results anywhere.** The cut is made only once
the batch has finished, but nothing is accumulated while it runs:

1. When a case finishes, its runner renders that case's results document — the exact text that
   will appear in the output — and stores it on S3 with its size. The runner then forgets it; its
   memory never holds more than the one conversation it is running.
2. The dispatcher adds the case's size, outcome and cost to the batch index (small blocks of
   1,000 cases).
3. When the last case finishes, the assembler **plans** the parts from the index alone — sizes
   only, no transcript is read — so the number of parts is final before anything is written.
4. Each part is then **streamed**: case documents are read from S3 one by one and appended to a
   multipart upload. Memory use is one case plus one 8 MB upload chunk, whatever the batch size.
   Parts are independent, so a large batch writes them in parallel, and a failed part is redone
   on its own.

The per-case documents are not a temporary copy — they are the durable case records the console
reads, and they let a batch be re-assembled later with a different part budget.

A **snapshot** of a running batch (`partial-results-<time>`) is built the same way from the cases
finished so far; its "of N" describes that snapshot only.

### 4.5 Batch index — `…__index.csv`

One row per case, for the whole batch: `case_id, key, mode, locale, persona, outcome,
stop_reason, user_turns, duration_s, errors, cost_eur, cap_eur, part`. Built from the same index
as the plan. It is for spreadsheets and scripts — and for finding which part holds a case —
rather than for LLM reading, so it is not split.

---

## 5. Reserved for later versions

These fields are part of the shape now so v2 and v3 need no format change; v1 rejects them with a
clear message.

| Field | Planned use |
|---|---|
| `channel: call` | Simulate a phone call through the call service's turn function. |
| `channel: whatsapp` | Run the WhatsApp path (objection gate, check-in scheduler) with the send step captured instead of delivered. |
| `rudi.engine: <release>` | Run a pinned Rudi release or prompt set side by side with `live`. |
| `script[].media` | Photos and voice notes, once Rudi can process them. |
| `persona.voice` | A synthetic voice for call simulation. |

---

## 6. Decisions to confirm

1. **YAML multi-document** as the canonical format, with JSON / JSONL accepted.
2. **ID and file-name scheme** in §2, including the Brussels calendar for the date part and a part
   marker on every results file.
3. **Outcome vocabulary** in §4.2, and `on_limit: pass` for guardrail probes.
4. **What counts as an attempt**: a user message the tester LLM itself marks as pushing for its
   objective.
5. **Identifiers are an error, not a warning** — synthetic data only, enforced at validation.
6. **`gap` (simulated clock) ships in v1.** The engine already accepts an injected clock; this
   lets the arena test next-day check-ins without waiting a day.
7. **Budgets** (§3.6): `batch.max_cost_eur` and `limits.max_cost_eur`, in euro of total spend (model calls plus AWS overhead),
   both optional with arena defaults; `over_budget` as its own outcome so a budget stop is never
   mistaken for a Rudi failure.
8. **Results split only after the batch finishes**, planned from per-case sizes and streamed
   (§4.4); snapshots carry their time; the header no longer lists every case of the batch.
