# Rudi's skills catalogue

Rudi's engine — WhatsApp, web chat and voice call — split into **37 separable skills** (plus one
*planned* skill, a target not built yet): what each
does, what good looks like, where it lives in the code, how a test sees it, and its known gaps.
It is the map the Training Arena's **Improvement Extractor** uses: every weak point it finds and
every feature request it writes cites one skill id, so a coding agent knows where to work.

- **Data (source of truth):** [`services/training-arena/seed/skills/rudi-skills.yaml`](../services/training-arena/seed/skills/rudi-skills.yaml)
  (`format: rudi-skills@1`), seeded to `s3://<arena bucket>/skills/rudi-skills.yaml` by
  `python deploy.py training-arena`. This page summarises it; the YAML has every field.
- **Used by:** `services/training-arena/src/arena_improve.py` — in AWS (`meetrudi-arena-extractor`)
  and on a user's own Claude (the local generator). See [training-arena.md](training-arena.md) §13a.
- **Kept true by:** `services/training-arena/tests/test_improve.py` — ids unique, domains known, and
  every `code` and `tests` pointer (file, and symbol where given) must exist.

## Fields of a skill

| Field | Meaning |
|---|---|
| `id` | Stable kebab-case id — what feature requests cite. Never rename one; add and retire instead. |
| `name`, `domain` | Display name; one of the domains below. |
| `channels` | `whatsapp` · `webchat` · `call` |
| `summary` | What Rudi does, in plain words. |
| `expected` | What good looks like — what a judge or a test checks. |
| `observe` | Where a test or a reader sees it (signals, result fields, logs, manifests). |
| `code` | `path` or `path:symbol` from the repository root. |
| `knobs` | Limits and settings that shape it. |
| `tests` | `path` or `path::Class_or_test`. |
| `arena` | `chat` when the arena's chat channel exercises it, else `none`. |
| `status` | `planned` for a target that is not built yet, so findings have a home; absent otherwise. |
| `gaps` | Known shortfalls — the first things to test. |

## The skills

Channels: WA = WhatsApp, web = web chat (tester console, Training Arena, test console, try-rudi
page), call = voice call. Arena ✓ = the Training Arena's chat channel exercises it today.

### Conversation flow

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `first-contact-rapport` | **First contact — getting to know the person.** *Planned, not built:* in the first conversation Rudi gets to know the person before advising, every reply gives something back, at most one question per reply ([Gym 01](../evals/skill-briefs/gym-01-first-contact.skill.md)). | WA · web · call | ✓ | `rudi_learn_prompt_wa.md` |
| `conversation-phases` | **Conversation phases.** Moves the person through learn → goal → commit → committed (the check-in loop) → concluded. | WA · web · call | ✓ | `responder.py:respond` |
| `session-opening` | **Session opening and returning greeting.** A brand-new contact gets the canned intro; a contact whose session concluded gets the canned welcome-back — neither calls the model. | WA · web | ✓ | `responder.py:respond` |
| `learn-about-rudi` | **Who Rudi is, and the intent to try.** Answers questions about Rudi from the "About me" context — warm, first person, short — and describes the channel truthfully. | WA · web · call | ✓ | `rudi_learn_prompt_wa.md` |
| `goal-setting` | **Goal setting — elicit, clarify, classify.** Understands ONE self-directed goal without re-asking what was already said; at most one clarifier per turn and two in total; on acceptance restates the goal in one line and reports goal_status=accepted, goal and goal_domain (diabetes, fitness, diet, sleep, stress, habit, other). | WA · web · call | ✓ | `rudi_goal_prompt.md` |
| `goal-scope-rejection` | **Out-of-scope goals.** Refuses goals about other people or the world, and illegal or harmful goals, with a short firm nudge, and asks for a personal goal (goal_status=rejected). | WA · web · call | ✓ | `rudi_goal_prompt.md` |
| `commitment-securing` | **Securing a commitment.** Proposes ONE small, specific, realistic action and nudges warmly, without pressure or shame. | WA · web · call | ✓ | `rudi_commit_prompt.md` |
| `check-in-follow-up` | **The check-in loop (committed phase).** Follows up on the agreed action by name: done earns credit, not done earns curiosity, never disappointment; agrees the next small step; changes the goal only when the person asks. | WA · web | ✓ | `responder.py:CHECKIN_PROMPT` |
| `check-in-promise` | **Promising the next contact.** At every natural pause Rudi says when HE will check in and reports check_in_minutes and check_in_about. | WA · web · call | ✓ | `responder.py:CHECKIN_NOTE` |

### Coaching content

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `health-coaching-guidance` | **Lifestyle-only health coaching.** For health-domain goals the commit phase gets 15 coaching rules: autonomy, change talk, a SMART step, one obstacle, light tracking; no meal or exercise plans, no supplements; clinical topics go to the doctor; red flags (chest pain, stroke signs, very high or low sugar, confusion) mean urging prompt professional or emergency care. | WA · web · call | ✓ | `health-coaching-guidance.md` |

### Safety and guardrails

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `medical-advice-refusal` | **No medical advice.** No diagnosis, medication or treatment instructions, no interpreting results; stays with lifestyle adherence and points to the clinician. | WA · web · call | ✓ | `rudi_guardrails.md` |
| `crisis-signposting` | **Crisis and self-harm signposting.** On any self-harm or crisis signal Rudi stops coaching and gently shares https://findahelpline.com/countries/be; never engages, encourages or assists. | WA · web · call | ✓ | `rudi_guardrails.md` |
| `ai-identity-disclosure` | **Saying he is an AI.** In chat, when asked, honestly says he is Rudi, an AI coach, never human. | WA · web · call | ✓ | `rudi_guardrails.md` |
| `scope-and-tone-boundaries` | **Scope and tone boundaries.** Stays on the person's goal (no general-assistant tasks); never bullies, shames, guilt-trips, threatens or blackmails; never promises results; replies in 1–4 sentences in the person's language. | WA · web · call | ✓ | `rudi_guardrails.md` |
| `confidentiality-injection-resistance` | **Confidentiality and prompt-injection resistance.** Instructions, notes, stored details and internals are confidential. | WA · web · call | ✓ | `rudi_guardrails.md` |
| `prompt-leak-guard` | **Reply leak gate (deterministic).** Blocks any reply that contains an internal marker or 10+ consecutive words copied from the system prompt (quoted user words and the public About-me text are exempt) and sends a localized redirect instead ("Let's keep the focus on you…"). | WA · web · call | ✓ | `leakguard.py:guard` |
| `objection-gate` | **Objections — wrong number, stop, hostile (freeze).** Before the model sees a turn, a lexicon (all locales) and — on the first 5 user messages — a model classifier detect objections. | WA | — | `objection.py:detect` |
| `pii-deidentification` | **PII redaction and aliasing.** Tier 1, irreversible, at ingest: Belgian national number, e-mail, IBAN, card and phone become placeholders. | WA · web · call | ✓ | `deid.py:scrub_inbound` |

### Language, tone and time

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `language-mirroring-and-i18n` | **Language mirroring and localized strings.** Replies in the person's language and reports signals.lang; the last-used language becomes the contact locale for canned strings and proactive messages. | WA · web · call | ✓ | `responder.py:LANG_NOTE` |
| `persona-ocean` | **Personality (OCEAN).** Tone, energy, warmth and pacing follow the conversation's OCEAN persona block (placed after the guardrails, before the phase body). | WA · web | ✓ | `personality.py:resolve_block` |
| `chat-message-style` | **Message style and channel self-description.** Short, human WhatsApp-style messages (1–4 sentences, *bold* not **bold**); reach-outs 1–2 sentences of plain text; says he lives on WhatsApp, never that he is a website or app; acknowledges media it cannot read yet. | WA · web | ✓ | `responder.py:CHANNEL` |
| `time-awareness` | **Time awareness.** Knows the local time and talks about moments like a friend ("today by 18:00", "last Tuesday"); never echoes timestamps or time markers; checks that a planned time has passed before asking how it went; summaries keep full dates. | WA · web · call | ✓ | `timeline.py:now_note` |

### Memory and state

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `conversation-memory` | **Session memory and goal continuity.** Remembers the last 20 turns (24 on calls), the goal, its domain and the commitment with timestamps; the goal survives a session restart; history is cleared when learn switches to goal and on a concluded restart. | WA · web · call | ✓ | `responder.py:new_session` |
| `profile-summary` | **Profile summary (most recent development).** After each committed or concluded turn — and before a nudge when stale — writes a one-sentence neutral third-person summary with full dates, plus the goal, to profile.json. | WA | — | `responder.py:summarize` |

### Scheduling and proactive contact

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `check-in-scheduling` | **Check-in scheduling.** A promised check-in outranks the window heuristics: it is sent at the promised time, moved to social hours and the 45-minute spacing floor, if that lands before the 24 h window closes. | WA | — | `store.py:compute_next_proactive` |
| `keep-warm-reach-out` | **Proactive message content.** Writes one short warm message about the person's goal or last step — about THE promised thing when there is one; no re-introduction, no new goals or homework; canned nudge on AI error. | WA · call | — | `responder.py:reach_out` |
| `outreach-limits-and-quiet-hours` | **Outreach limits and quiet hours.** At most 2 unanswered system-initiated sends in a row; at least 45 minutes after the last outbound; nothing proactive between 21:30 and 06:30 local time; consent, active status and keep_warm gate every proactive send; replies are never held. | WA · call | — | `store.py:outreach_block` |
| `out-of-window-templates` | **Out-of-window templates.** After the 24 h window lapses, sends a pre-approved template with {{1}} = the stored goal or a generic phrase; rotates template SIDs per locale; at least 48 h between templates; dormant after 3 unanswered. | WA | — | `reengage.py:_send_template` |

### Channel — WhatsApp

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `whatsapp-ingest-and-delivery` | **WhatsApp ingest and delivery.** Verifies the Twilio signature, queues per user (FIFO), pseudonymizes, stores every message masked, counts media; media without text gets a canned acknowledgement (no ASR or vision yet); errors get the canned "tired" / "error" strings; AI_RESPONDER=false hands the thread to an operator. | WA | — | `webhook.py:handler` |

### Channel — web chat

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `webchat-turn` | **Web-chat turn.** The WhatsApp engine minus delivery: de-identify, respond, restore. | web | ✓ | `webchat.py:chat_turn` |

### Channel — voice call

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `call-opening-and-greeting` | **Call opening.** Rudi speaks first with a greeting only (first name, a few words), does not yet say what he is or why he calls, and waits for "hello". | call | — | `brain.py:open_call` |
| `call-turn-taking-and-speech` | **Speaking on a call.** Speakable replies: 1–2 sentences (3 at most), about 40 words, one question placed last, no markdown or emoji, numbers as words; follows interruptions; asks for a repeat when unintelligible. | call | — | `brain.py:VOICE_STYLE` |
| `call-goals-and-whatsapp-handoff` | **Call goals and the WhatsApp hand-off.** Each call has a hidden system goal: GET_TO_KNOW (first call; ends by asking for any WhatsApp message), SET_NEARTERM_GOAL or GOAL_FOLLOWUP (second call), REINSTATE_TALK after 7 quiet days; runner call-backs for a promise, a WhatsApp reminder 22 h after GET_TO_KNOW, and recommit. | call | — | `brain.py:_goal_note` |
| `call-closing-and-time-budget` | **Ending a call.** The call ends the moment the person commits: that turn confirms the commitment, says when Rudi will be back in touch, thanks them by name and wishes them well. | call | — | `brain.py:FAREWELL` |
| `call-voicemail-and-failure-recovery` | **Voicemail and failure recovery on calls.** Never talks to a machine: Twilio AMD plus a screen of the first utterance (≥60 characters and a voicemail phrase means a silent hang-up). | call | — | `relay.py:looks_like_voicemail` |
| `call-dispatch-and-follow-up-calls` | **Placing calls.** Dials only with consent, an active contact, outside quiet hours and to the registered number; tester limits (5 connected calls each, 40 a day, one at a time); at most one Rudi-initiated reach-back; a speak-only call when the AI has no headroom; a frozen WhatsApp thread cancels every pending call. | call | — | `dispatcher.py:place_call` |

### Platform (not a Rudi behaviour; where application errors are located)

| Skill id | What Rudi does | Channels | Arena | Main code |
|---|---|---|---|---|
| `model-gateway` | **Model gateway and the JSON reply envelope.** Every engine turn calls the model cascade from S3 config/ai_endpoints.json (Groq and OVH gpt-oss-120b, then smaller models) in JSON mode, retries a 429 twice with backoff, and parses the {reply, signals} envelope; an unparsable answer degrades to plain text with no signals. | WA · web · call | ✓ | `gateway.py:generate` |
| `arena-harness` | **The Training Arena itself.** Not Rudi: the simulated tester, the judge, the runner, the dispatcher and the results writer of the Training Arena. | web | ✓ | `runner.py` |

## How a turn flows (where the skills sit)

**Phases** (`conversation-phases`) — chat engine, `responder.respond` / `_advance`:

```text
(no state) --any msg--> canned intro (no model call)                          -> learn
learn      --want_to_try--> goal (history cleared)
goal       --accepted--> commit · --rejected x3--> concluded · --unclear x2--> last text force-accepted -> commit
commit     --commitment_made--> committed · --7 attempts--> concluded
committed  --commitment_made--> committed (new step) · --accepted + goal--> commit (new goal)
concluded  --any msg--> canned welcome-back (no model call) -> learn, goal kept
```

On a call (`brain.advance`) a commitment ends the call (`concluded`) and the time budget can force
it; there is no `committed` phase.

**One WhatsApp turn** — `webhook` (signature, per-user FIFO) → `processor`: pseudonymous user id →
alias vault for the 24 h window → Tier-1 redaction and Tier-2 aliasing (`pii-deidentification`) →
store and open the window → frozen? stop → `objection-gate` → media without text gets a canned
acknowledgement → `responder.respond`: system prompt = guardrails + OCEAN persona + phase body
(+ About-me, or health guidance in commit, or the check-in prompt) + channel, language, check-in and
time notes; the last 20 turns with time markers; `gateway.generate` in JSON mode → envelope parsed →
time markers stripped → `prompt-leak-guard` → phase advanced → bold converted → placeholders
restored → sent → check-in promise stored and the next proactive contact scheduled → profile
summary on committed or concluded turns. A web-chat turn (`webchat-turn`) is the same minus
delivery, the objection gate and scheduling.

**Guardrail stack, in order:** objection gate (WhatsApp) → de-identification → prompt guardrails
(lead every phase prompt) → health guidance with red flags (commit phase, health domains) → the
deterministic leak gate → restoring placeholders. Crisis handling is prompt-only — there is no
classifier on the live path.

## Known gaps — likely test targets

- `conversation-phases` — A reply in prose without the JSON envelope carries no signals, so the phase silently stalls.
- `session-opening` — The intro is always English, even for nl-BE users. The message that triggers the intro or the welcome-back is neither answered nor stored — a crisis disclosure at that moment gets "👋 Hey again!".
- `learn-about-rudi` — Misses soft intent; on WhatsApp may describe itself as a website or app.
- `goal-setting` — A force-accepted goal can be a fragment of the last message; a wrong domain silently drops the health guidance.
- `check-in-follow-up` — The committed phase gets no health guidance (it is injected in the commit phase only).
- `check-in-promise` — Promises beyond 24 h or inside quiet hours are dropped at capture, so they silently never happen. In web chat the promise is made but nothing is ever delivered.
- `health-coaching-guidance` — WhatsApp reads contexts/diabetes-t2d-guidance.md, which is not in the repository (live bucket only), so its coaching content can drift from health-coaching-guidance.md. Red-flag guidance reaches the model only in the commit phase.
- `medical-advice-refusal` — No unit test asserts refusal; no deterministic or classifier gate (project.md §6 describes one).
- `crisis-signposting` — Only the directory link is prompted; named lines (1813, 112, GP) are rare. A disclosure as the first message, or after concluded, gets the canned greeting. Groq can reject Rudi's JSON on explicit suicidal statements, so these turns fall back to OVH with 10–25 s latency.
- `ai-identity-disclosure` — Calls that start in learn (GET_TO_KNOW, REINSTATE_TALK) never get the disclosure block, yet are marked disclosed.
- `scope-and-tone-boundaries` — Prompt-only; no unit tests.
- `confidentiality-injection-resistance` — The try-rudi web page's learn phase has no guardrails (services/rudi-chat/src/app.py).
- `objection-gate` — Indirect objections after turn 5 reach only the lexicon. Not exercised by the arena's chat channel.
- `pii-deidentification` — Proactive reach-outs are sent without restore_outbound, so a model-echoed placeholder could reach the phone. The test console does not de-identify.
- `language-mirroring-and-i18n` — The intro is always English (see session-opening).
- `persona-ocean` — The call engine has no persona block.
- `chat-message-style` — In web chat (tester console, arena) Rudi still says "here on WhatsApp" and promises check-ins that never come.
- `conversation-memory` — profile.json (goal, most recent development) feeds reach-outs only, never replies — the nudge remembers what the reply forgets.
- `keep-warm-reach-out` — Sent without restoring aliases (see pii-deidentification).
- `out-of-window-templates` — No template SIDs are configured, so nothing is sent after the window lapses.
- `webchat-turn` — The arena chat skips the objection gate, delivery, the 24 h window, re-engagement and the profile.
- `call-goals-and-whatsapp-handoff` — REINSTATE_TALK has no instruction note.
- `call-closing-and-time-budget` — The tester configs send max_seconds=300 but the brain reads max_minutes, so the 5-minute cap is not enforced; learn-phase calls get no wrap-up warning.
- `model-gateway` — Groq rejects some of Rudi's JSON on explicit crisis statements, which costs a fallback hop.

## Keeping it current

A remodel of the catalogue is planned (agreed 2026-10-23); until then, new targets are added as `status: planned`.

When code that a skill points to moves or changes behaviour, update the YAML in the same change and
bump `version` (reports record the version they were made with). The tests fail when a pointer no
longer exists. Retire a skill rather than renaming its id: old reports cite it.
