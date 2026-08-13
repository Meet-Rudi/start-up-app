# Memory, Time & De-identification — design blueprint

> Status: **proposal**, not yet implemented. Code draft for parts B and C exists at
> `services/whatsapp/src/deid.py` (+ `tests/test_deid.py`, 22 passing).
> Owner: engineering. Touches Prime Directives §0.1, §0.3 and §5 — read those first.
> Written 2026-08-13 against commit `7951e48`.

Three problems, one pipeline. Testers report Rudi is amnesiac and time-blind (**A**). Separately we
must strip Belgian national numbers before storage (**B**) and mask indirect identifiers before the
model sees them (**C**). B and C are privacy work, A is product work — but all three change the same
few lines where text enters and leaves the engine, so they are designed together.

---

## Part A — Why Rudi has no memory and no clock

### Diagnosis

This is not prompt tuning. Four concrete mechanisms in the current code produce exactly the
behaviour testers describe.

**1. Session end wipes everything.** When the machine reaches `concluded`, the next inbound calls
[`new_session()`](../services/whatsapp/src/responder.py#L142-L145), which returns
`{"phase": "learn", "history": [], …}`. The entire conversation history is discarded.

**2. The durable profile is never injected into a reply.**
[`respond()`](../services/whatsapp/src/responder.py#L246) builds its system prompt from
`_build_system(phase, note_state, personality_block)` — guardrails, persona, phase prompt. It never
reads `profile.json`. Meanwhile
[`reach_out()`](../services/whatsapp/src/responder.py#L197-L217) *does* receive `goal` and
`development`, fed by [reengage.py:117-120](../services/whatsapp/src/reengage.py#L117-L120).

> **The asymmetry is the bug.** Rudi's *unprompted* check-ins remember the person. Rudi's *answers
> to the person* do not. A tester who replies to a warm, informed nudge gets a blank slate back.

**3. No temporal signal reaches the model at all.** History entries are `{role, content}` — the `at`
timestamp is dropped when messages become prompt turns. No current date, no elapsed time, no
day-of-week. The model cannot distinguish five minutes from five weeks, so it cannot say "last
week you told me…" without hallucinating.

**4. The returning-user greeting is a canned string.** A concluded conversation re-greets with
`i18n.t("welcome_back", locale)` — pleasant, and entirely memoryless.

The data we need already exists and is already persisted. `ContactMeta` carries `created_at`,
`last_inbound_at`, `timezone`, `next_proactive_at`, `msg_total`; every message is stored under a
millisecond-timestamped key. **None of it reaches the prompt.** This is a plumbing gap, not a
storage gap — which is why it is cheap to fix.

### Fix, in three layers

Ordered by value-per-effort. Layer 1 alone will resolve most of the "no sense of time" complaint.

#### Layer 1 — Temporal grounding block (no model call, deterministic, testable)

Prepend to every generation, computed from `ContactMeta` in the contact's own timezone:

```
[Now] Thursday 13 August 2026, 19:42 (Europe/Brussels).
[Since] You last spoke 12 days ago (Friday 1 August). You have known this person
        3 months (first message 14 May 2026), across 47 messages and 6 conversations.
[Next]  Your next planned check-in is in 2 days.
```

And stamp history turns that are not from the current sitting:

```
[3 days ago] user: I managed the walk on Tuesday but skipped Thursday
[just now]   user: honestly this week was a mess
```

Roughly 60 tokens. It is the difference between "How are you?" and "Twelve days is a long gap —
what got in the way?". `[Next]` is what gives Rudi a sense of the *future*: it already knows when it
intends to reach out again, it simply has never been told.

#### Layer 2 — A durable dossier, injected on every turn

Promote `profile.json` from a counters record into the memory Rudi actually reasons over, and inject
it into `_build_system` for **all** phases — not just reach-outs. Proposed additions:

| Field | Purpose | Written when |
|---|---|---|
| `commitment: {text, made_at, due_at, status}` | The concrete future hook. Today a commitment is captured as prose with no date, so nothing can ever ask "how did Tuesday go?" | on `commitment_made` |
| `session_log: [{ended_at, summary, outcome}]` | Last ~5 sessions, one line each. Cross-session continuity. | at `concluded` |
| `facts: [{key, value, first_seen, last_confirmed}]` | Bounded durable truths: works shifts, has a dog, hates mornings. Cap it (~15) and expire by `last_confirmed`. | rolling |
| `open_threads: [string]` | Things left hanging, so Rudi can reopen them. | at `concluded` |

Rendered as a `# What you already know about this person` block. Budget ~200-300 tokens — well
inside the §4 cost posture, and far cheaper than the re-asking it eliminates.

The write hook already exists: `_update_profile()` fires at `concluded`
([processor.py:61-73](../services/whatsapp/src/processor.py#L61-L73)). It needs richer extraction,
not new plumbing.

**`due_at` is the single highest-value field here.** It converts Rudi from a system that reacts to
one that anticipates, and it is what `reengage.py` should schedule against.

#### Layer 3 — Continuity and recall

- **Don't wipe.** `new_session()` should seed history with a one-line recap of the previous session
  rather than `[]`.
- **Memory-aware greeting.** Replace the canned `welcome_back` with a generated opener grounded in
  the dossier + temporal block.
- **Vector recall** (§2 `MemoryStore`, §4 priority order) for anything older than the session log.
  Deliberately last: layers 1-2 cover the reported complaints, and pgvector is a bigger lift.

Per §4 the assembly order stays: **structured profile → cached recent summary → vector recall**.

---

## Part B — Tier 1: irreversible redaction at ingest

**Rule: direct identifiers never get written down.** Redacted at the door, before persistence,
before logging, before the operator console. No key is kept — the original is unrecoverable by
design, which is the entire point: what we never stored cannot leak, cannot be exported, and cannot
be subpoenaed out of us.

Implemented in [`deid.redact()`](../services/whatsapp/src/deid.py). Covers the Belgian national
number (RRN/Rijksregisternummer), email, IBAN, payment card, phone.

### The national number, precisely

Layout `YY.MM.DD-SSS.CC` — birth date, sequence, **mod-97 checksum**. Accepted written forms:
`85.07.30-033.28`, `85073003328`, `850730 033 28`, hyphen/slash/space variants.

The checksum is doing critical work. A bare `\d{11}` regex would eat phone numbers, order
references, step counts and amounts — silently corrupting conversations to protect nothing. So
`redact()` validates before replacing:

- **Two checksum variants.** Born before 2000: `CC == 97 − (first9 mod 97)`. Born 2000 or later: a
  `2` is prefixed before the modulo. We cannot know the century, so either passing is a match.
- **Bis-numbers.** Non-residents and unestablished birth dates carry month **+20 or +40** (so `47`
  means July). Handled — these are common in a Belgian health context and a naive month check would
  miss exactly the population most sensitive about identification.

Everything matched becomes `<| users-national-id |>`.

Tested both ways: valid numbers in five written formats redact; bad checksums, `12000 steps`,
`82.5 kg`, `10.30`, `glucose was 7.8` all survive untouched.

### Where to call it

Every ingress, before anything else:

| Point | File | Note |
|---|---|---|
| WhatsApp inbound | `webhook.py`, before the SQS enqueue | earliest possible; keeps it out of the queue body too |
| Test console | `test_console.py::_send` | testers paste realistic data |
| ASR transcripts | `services/voice-bench` | spoken numbers land here |
| Consent intake | `services/registration` | a form is where an RRN is *most* likely |
| Display name | `store.record_inbound` | WhatsApp profile names are user-controlled free text |

Also apply to **outbound** text: if the model ever echoes a redaction back it must not be re-expanded
by anything downstream.

> **Accept the trade-off knowingly:** because this runs before storage, the operator console and
> exports will show `<| users-national-id |>` too. That is correct — but it means an operator can
> never recover a number a user legitimately needed to share. If any workflow requires that, it needs
> a separate, consciously-designed path, not a weakening of this one.

---

## Part C — Tier 2: reversible pseudonymization at the model boundary

**Rule: quasi-identifiers stay inside the AWS-EU plane and are masked on the wire.** Names of other
people, places, employers, clinicians. These cannot be redacted like Tier 1, because Rudi must be
able to say "Peter" back to the user.

```
inbound ──redact (tier 1)──▶ store  (AWS EU, raw names — permitted by §0.1)
                               │
                               ├── vault.mask (tier 2) ──▶ MODEL (3rd-party API / GPU plane)
                               │                              │
                  WhatsApp ◀── vault.unmask ◀─────────────────┘
```

Round trip, exactly as specified:

```
user:  "and I went running with Peter today"
model: "and I went running with <| Person_A |> today"
reply: "and did <| Person_A |>'s presence improve your results?"
user:  "and did Peter's presence improve your results?"
```

### Make it non-bypassable

Put the mask/unmask sandwich **inside `gateway.generate()`**, not in the callers. `gateway` is the
single choke point every model call already passes through — `respond()`, `reach_out()`,
`summarize()`, and anything added later. Guardrails are treated as non-bypassable in §6 for the same
reason; masking earns the same status. If it lives in the caller, someone will add a fourth call
site and forget.

```python
def generate(messages, json_mode=False, vault=None, detector=None):
    if vault is not None:
        messages = [{**m, "content": vault.mask(m["content"], detector)} for m in messages]
    result = _cascade(messages, json_mode)          # existing logic, untouched
    if vault is not None:
        result["text"] = vault.unmask(result["text"], fallback="they")
    return result
```

Unmask the **raw** response before `_parse_envelope()` — that way `signals.goal`, which flows into
the durable profile, comes back clean too.

### Vault lifecycle

`AliasVault` lives at `conversations/{uid}/aliases.json` — a separate object on purpose:

- **Erasure (§5)** is one delete, and it is easy to prove it cascades.
- It never rides along in a transcript export.
- A TTL can expire it independently of the conversation.

Created on first masked turn, dropped when the session concludes — matching the "retain for the
duration of the talk, then drop it" requirement.

### The tension you should decide on explicitly

**Part A wants durable memory. Part C wants the key dropped.** If the vault is dropped at session
end and the dossier stores `"went running with <| Person_A |>"`, then next month that memory is
unreadable — Rudi either says a raw placeholder to the user or forgets the detail entirely.

**Proposed resolution: the dossier stores roles, never names.** Alongside each alias the vault keeps
a non-identifying descriptor, and *that* is what survives:

```json
{ "alias": "Person_A", "role": "running partner" }
```

The names die with the session; the relationships persist. Rudi later says *"how did the run with
your running partner go?"* — marginally less intimate, fully clean, and no placeholder can ever
surface. `AliasVault.roles` and `set_role()` implement this, and a test asserts a role-only vault
still resolves a placeholder after the names are gone.

Better still, the role can be captured **without ever exposing PII**: the model sees only
`<| Person_A |>`, so asking it to emit `signals.roles = {"Person_A": "running partner"}` is a
description of a placeholder, not of a person.

### Detection, and its honest limits

`HeuristicDetector` — zero dependencies, Lambda-safe — combines a **trigger grammar**
(`with X`, `my friend X`, `X said`) with an optional first-name gazetteer, biased hard toward
precision. The reasoning: a false positive mangles a real word into a placeholder and the user *sees*
Rudi talking nonsense; a false negative leaks one first name to the model — bad, bounded, and
catchable by an eval rather than by a tester.

Two failure modes were found by probing and fixed, both worth knowing about because they are the
shape of bug this approach produces:

- Case-insensitive matching made `[A-Z]` match lowercase, so *"I went with my wife **to** the
  hospital"* detected `to` as a person. Case-insensitivity is now scoped to the trigger words only.
- *"I spoke with **Doctor** Smith"* captured the title and left the real name exposed. Titles are now
  consumed but not captured → `with Doctor <| Person_A |>`.

Both are regression tests. **This detector will still miss names**; it is a pilot-grade component
behind the `PersonDetector` seam (§2) precisely so spaCy, Presidio, or a self-hosted NER on the GPU
plane can replace it without touching a single caller.

Not yet covered, and worth a decision: **place names, employers and clinic names** (`Place_A`,
`Org_A` follow the same mechanism), and the fact that a rare condition plus a small town is itself
identifying.

---

## ⚠ This is more urgent than it looks

The live model cascade (`config/ai_endpoints.json` in the data bucket, read 2026-08-13) is:

| Endpoint | Region | Enabled |
|---|---|---|
| `mistral-small` (api.mistral.ai) | EU (France) | **disabled** |
| `groq-llama-3.3-70b` (api.groq.com) | **US** | enabled |
| `groq-llama-3.1-8b` (api.groq.com) | **US** | enabled |
| `groq-fallback` (hardcoded in `gateway.py`) | **US** | always appended |

Every message a tester sends — names, health talk, everything — currently goes **raw to a US API**.
That sits against §0.1 (third-party AI APIs receive only minimized, pseudonymized payloads) and §0.3
(EU-region model endpoints only).

I assume this is a known interim for the pilot rather than an oversight — but it should be an
explicit, recorded decision, not an implicit one, and it makes Part C a prerequisite for real users
rather than a refinement. Two independent mitigations, ideally both:

1. Ship Tier 1 + Tier 2 before onboarding non-synthetic conversations.
2. Enable the Mistral EU endpoint ahead of Groq in the cascade, and confirm the DPA.

---

## Rollout

Sequenced so each phase is shippable and independently verifiable.

| Phase | Scope | Gate |
|---|---|---|
| 0 ✅ | `deid.py` + 22 unit tests | done |
| 1 | Tier 1 at all ingress points | leak-eval on a synthetic corpus: **zero** direct identifiers stored |
| 2 | Tier 2 in `gateway.generate()` + vault persistence | round-trip eval; assert `has_placeholder()` is False on every outbound |
| 3 | Temporal grounding block | in-window / out-of-window tests (§8); no model cost |
| 4 | Dossier + `due_at` + memory-aware greeting | prompt eval suite before ship (§8) |
| 5 | Vector recall via `MemoryStore` | separate design |

Per §8, phases 2-4 change the live send path and prompts, so none ship without their eval suite.
Two assertions belong in the send path permanently:

- **No raw placeholder ever reaches a user.** `deid.has_placeholder(text)` must be False before
  `provider.send_text`. Fail closed — send the fallback rather than the markup.
- **No PII in logs (§5).** The existing `print` lines are already metadata-only; keep it that way.

Also add `aliases.json` to the right-to-erasure cascade (§5) — it is a new store and erasure must be
complete, not best-effort.

---

## Decisions I need from you

1. **Role-based durable memory** — confirm the resolution above (names die with the session, roles
   persist), or say you'd rather keep a long-lived per-contact vault for full fidelity.
2. **Operator visibility** — Tier 1 redaction is invisible to operators by design. Any workflow that
   breaks?
3. **Place/org masking** — in scope for the pilot, or persons only for now?
4. **Groq vs Mistral** — is the US cascade a recorded interim decision, and should Part C gate the
   first non-synthetic users?
5. **Where `deid` lives** — it is in `services/whatsapp/src/` so it deploys today. Per §1 shared
   Python belongs in `/packages`; it needs promoting to a Lambda layer once `voice-bench` and
   `registration` consume it.
