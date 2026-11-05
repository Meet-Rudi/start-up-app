# meetrudi-call — Plan A outbound calling

A real phone call: Twilio dials the patient, ConversationRelay owns speech in both directions,
and an API Gateway WebSocket carries JSON text to a per-message Lambda running the **same**
`learn / goal / commit` conversation as the bench and WhatsApp — on `brain.py` or, per call, on
CM's clone on the platform Turn Engine (see [Two engines](#two-engines)).

No Fargate, no always-on process, no GPU. The socket is held by API Gateway; the call's state
lives in S3.

## Shape

```
EventBridge / manual
      │
      ▼
meetrudi-call-dispatcher ──── gates: consent · active · quiet hours · number
      │                       creates calls/{call_id}/manifest.json
      │                       POST /Calls  (Twiml carries call_id as a <Parameter>)
      ▼
Twilio (IE1) ── dials ──▶ patient's phone
      │
      │ on answer: wss:// to meetrudi-voice-ws
      ▼
meetrudi-call-ws     $connect     accept
                     $default     setup | prompt | interrupt | dtmf | error
                     $disconnect  finalise
      │
      ▼
brain.py  ── guardrails → phase machine → reply text → Twilio speaks it
```

**Why a per-message Lambda works here:** ConversationRelay sends **JSON text, never audio** —
roughly 20–40 messages per call, not 50 audio frames per second. Each invocation handles one
utterance and exits. Media Streams would have needed a persistent process; this does not.

## What Twilio gives us for free

The bench had to build these by hand in the browser. Here they are TwiML attributes, set in
`relay.DEFAULT_VOICE_ATTRS`:

| Attribute | What it replaces |
|---|---|
| `speechTimeout` | the bench's `silenceMs` endpointing |
| `interruptible="any"` | the hand-rolled barge-in |
| `reportInputDuringAgentSpeech` | recording through Rudi's turn so interruptions aren't lost |
| `hints` | the Whisper prompt seeded with name and topic |
| `ttsProvider` / `voice` | **Piper is not used here** — Twilio owns TTS on a real call |

## Voices

Set per language in `relay.VOICE_PROFILES`, as an **ordered cascade** rather than a single value.

| Language | Locale | Voice |
|---|---|---|
| `en` (default) | `en-US` | `en-US-Journey-D` (Google, male) |
| **`nl`, `nl_BE`** | **`nl-BE`** | **Luk Belcer — `ppGIZI01uUlIWI734dUU` (ElevenLabs, male) — pinned** |
| **`nl_NL`** | **`nl-NL`** | **Luk Belcer — same voice, pinned** |
| `fr` | `fr-BE` | Twilio default (Wallonia, not `fr-FR`) |
| `de` | `de-DE` | Twilio default |

**Every Dutch-speaking locale is pinned to Luk Belcer and has no cascade tail.** He was
hand-picked and listened to, so no other voice may stand in for him: a silent substitution would
put an unvetted voice in front of a patient, and the pilot would end up measuring a voice nobody
chose. Only the *locale* differs between `nl-BE` and `nl-NL` — that drives Deepgram's
transcription and the TTS language hint, not the timbre.

> **If a FEMALE Dutch voice is ever needed** — a second persona, an operator preference, or a
> patient who reacts better to one — the selected option is **Amazon Polly "Lisa" (`nl-BE`)**,
> the first purpose-built synthetic Flemish voice. Add it as a cascade entry with
> `{"ttsProvider": "Amazon", "voice": "Lisa"}`; the machinery already supports it and nothing
> else needs to change.

Locales with no chosen voice deliberately leave `voice` unset and let Twilio pick for the
locale. A guessed voice ID fails at **dial** time — the worst possible place to discover a typo
— so only voices that have actually been heard on a call are named here.

### Why the cascade is resolved before dialling, not during

ConversationRelay's `language` message can switch `ttsLanguage` and `transcriptionLanguage`
mid-call, but it accepts **neither `voice` nor `ttsProvider`** — those are fixed in the TwiML
when the call starts. There is no failover once someone has answered.

There is also no Twilio API that validates a ConversationRelay voice ahead of time, so a literal
"test the voice before calling" is not available. What exists instead is a **health record** at
`calls/_voices/health.json`, which is the closest honest equivalent:

- the dispatcher picks the first cascade step not known to have failed, and stamps `voice_key`
  on the call;
- a `type: error` frame mentioning TTS (Twilio 64111/64112) marks that voice failed;
- a turn Rudi actually speaks marks it healthy.

So a misconfigured voice costs **one** call rather than all of them, and recovery is automatic.
Unrelated errors never blame the voice. To force a retry of a demoted voice, delete its entry
from `health.json`.

Cascades that have a tail end in `{}` — no provider, no voice — because the last resort must be
something that cannot itself be misconfigured.

**Dutch is the exception: it is pinned, so it has no tail and no failover.** A single-entry
cascade is exhausted by the loop and returned anyway, so Luk is dialled however sick he looks.
The health record is still written — it stays the place to find out he has stopped working — but
nothing acts on it. The trade is deliberate: if ElevenLabs breaks, Dutch calls go **silent**
rather than quietly switching voice, and dead air is the failure somebody actually notices.

## Two engines

`ws.py` runs each call on one of two engines (decision #38, [`src/platform_call.py`](src/platform_call.py)):

| Engine | What runs | Where |
|---|---|---|
| `legacy` | `brain.py`, as since August | the function's own code |
| `platform` | CM's clone version 1 on the Turn Engine, held to `brain.py` turn by turn by `packages/rudi_platform/tests/test_voice_parity.py` | layer `meetrudi-tenant-prod-pool-engine`, built from `packages/` by [`build_layer.py`](build_layer.py) |

- **Which one:** the call config's `engine` if it names one, else `CALL_ENGINE` in the template
  (`platform` since 2026-11-05). Rolling back is `CALL_ENGINE: legacy` in `template.yaml` and
  `python deploy.py call`.
- **Fixed per call.** Chosen when the call is answered and stored as `manifest.engine`; every
  turn record carries it too. A deploy or a flipped switch never moves a call mid-way.
- **Never the reason a call fails.** If the platform cannot load, or its opening breaks for any
  reason other than the models, the call runs on legacy (`manifest.engine_fallback` says why).
- **Same telephony.** Voicemail screening, the lost-thread and pause lines, the ledger and the
  voice cascade are `ws.py`'s and `relay.py`'s for both engines.
- **Models.** Both read `config/ai_endpoints.json` every turn and add the Groq fallback. The
  platform tries the endpoints in `CALL_SLOW_MODELS` last (OVH gpt-oss-120b and Llama overran
  the 7 s attempt on calls), and can ask gpt-oss for a reasoning effort (`CALL_REASONING_EFFORT`,
  off: "low" was faster but invented names and tripped the leak guard more on simulated calls).
  A test call may pin endpoints with `models: [...]` in its config.
- **A turn needs the reply envelope.** On both engines, a turn's answer without the
  `{reply, signals}` JSON fails over to the next model (`gateway.generate(require_reply=True)`;
  the platform's voice profile sets `require_reply`), logged `failed: no-envelope`. OVH's
  gpt-oss-20b sometimes answers in prose naming internal fields; read as the reply, the leak
  guard replaced it with its redirect, on the AI-disclosure turn too (2026-11-05). The opening
  asks for plain text and is not affected; neither is WhatsApp.
- **Why a reply was replaced.** On the platform, a turn record whose reply the leak guard
  replaced carries `guard: {"leak": "marker" | "verbatim", "envelope": …}`. The envelope is
  `text` when the model answered in prose instead of the `{reply, signals}` JSON, or
  `json-no-reply:<keys>` when its JSON had no `reply`. Labels and key names only, never content;
  the same goes into the `ENGINE platform …` log line.

## The join key

`call_id` travels in the TwiML as `<Parameter name="call_id">` and comes back in Twilio's
`setup` message. It is the **only** thread linking a socket to a patient's record — without it
the handler would know a call had connected but not whose. The connection→call mapping is then
kept at `calls/_ws/{connectionId}.json`, because `prompt` messages carry no identity.

## Voicemail

**Twilio's Answering Machine Detection does not cover Belgium** (US and Canada only), so an
answered call and a voicemail box look identical. `relay.looks_like_voicemail()` screens the
**first utterance only** against greeting phrases in English and Dutch, and requires the
utterance to be **long as well as matching** — no short "hello?" can ever trip it.

Hanging up on a real patient is far worse than talking to an answerphone for six seconds, so the
bias is deliberate. Mid-call mentions of voicemail are ignored; only the first thing we hear is
screened.

## Deploy

```cmd
python deploy.py call
```

Then three one-time steps, all console-only because `rudi-deployer` cannot create IAM objects
or secrets:

**1. IAM** — add [`meetrudi-lambda-runner.call-add.json`](../../infra/iam/meetrudi-lambda-runner.call-add.json)
to the `meetrudi-lambda-runner` inline policy. An API Gateway WebSocket handler has no socket of
its own, so replying goes through the management API. **Without this the call connects and Rudi
stays silent** — verified: the handler runs correctly and only the reply is denied.

**2. Secrets** — Secrets Manager, plaintext JSON, region `eu-central-1`:

```
meetrudi/twilio/voice         {"account_sid":"AC…","auth_token":"…","from_number":"+32…"}
meetrudi/call/dispatch-token  {"token":"<python -c \"import secrets;print(secrets.token_hex(24))\">"}
```

Both fail closed: the dispatcher returns `503` until they exist.

**3. Twilio console** — nothing to configure. The TwiML is passed inline on each `POST /Calls`,
so there is no webhook URL to register and no TwiML Bin to keep in sync.

## Placing a call

Dry run first — builds the record and the TwiML, dials nobody:

```cmd
curl -X POST <Dispatch URL> -H "Content-Type: application/json" ^
  -d "{\"token\":\"<TOKEN>\",\"dry_run\":true,\"config\":{\"user_name\":\"Filip\",\"topic\":\"getting back to daily walking\",\"to\":\"+32470000000\",\"consent_state\":\"granted\"}}"
```

Drop `dry_run` to dial for real.

## Gates

Checked in this order, consent first:

| Gate | Why |
|---|---|
| `consent_state == "granted"` | CLAUDE.md §5 — consent gates processing |
| `status == "active"` | not archived, not blocked |
| outside 21:30–06:30 Europe/Brussels | the same quiet window the WhatsApp runner respects |
| a destination number | |

Refusing to dial is the safe failure, and each gate has a test asserting no call reaches Twilio.

## What lands in S3

```
calls/{call_id}/manifest.json        config, state, telephony, interruptions, outcome
calls/{call_id}/turns/{seq}.json     one object per turn
calls/_index/{YYYY-MM-DD}/{id}.json  one row per call
calls/_ws/{connectionId}.json        socket → call mapping, transient
```

**No audio is stored.** Twilio holds the media and we never receive it — the right default for
patient data: the transcript is what we need, the recording is what we would have to defend.

Note the prefix is `calls/`, separate from the bench's `voice-bench/`, so a retention or erasure
sweep can treat real patient calls differently without filtering.

## Testing without spending money

```cmd
python -m unittest discover -s services/call/tests -v
```

The tests drive a simulated ConversationRelay client through whole calls — the wire protocol,
every gate, the voicemail fallback, interruptions, a mid-call model failure, and the ordering
that ensures Rudi's goodbye is spoken *before* the hangup — on both engines. A real phone call
is an expensive, slow and irreversible way to find a bug.

## A whole call against the deployed stack, without a phone

```cmd
python services/call/rudi_sim_call.py --engine platform --lang nl --goal SET_NEARTERM_GOAL
python services/call/rudi_sim_call.py --show <call_id>
```

[`rudi_sim_call.py`](rudi_sim_call.py) invokes the deployed dispatcher (dry run: nobody is rung)
and `meetrudi-call-ws` with ConversationRelay's frames and a scripted caller: deployed code and
layer, S3 prompts, live models and keys, real time budget — everything but speech. Calls are
marked `simulated` and carry no tester id. `--show` prints any call's transcript, de-identified as
stored. `--models` pins endpoints, `--override-quiet-hours` simulates at night (audited).

## Voice check: does each voice speak its language?

```cmd
python services/call/rudi_voice_check.py --setup
python services/call/rudi_voice_check.py
python services/call/rudi_voice_check.py --teardown
```

Twilio speaks on a call and never sends us the audio, so [`rudi_voice_check.py`](rudi_voice_check.py)
records real calls with nobody on the line ([`src/voicecheck.py`](src/voicecheck.py)):

1. `--setup` points the account's spare Twilio number (not the caller id) at a check line that
   picks up and stays silent (`meetrudi-call-status?answer=voice-check`).
2. Each check is an ordinary ConversationRelay call to that number, `voice_check: true`, with
   Twilio recording on, one side per channel. **The dispatcher records nothing else**: a voice
   check to any number that is not the account's own is refused.
3. Rudi says a fixed sample sentence in the call's language (the welcome greeting, in the voice
   under test), then his opening from the engine.
4. The status Lambda keeps the WAV (`calls/{id}/audio/recording.wav`) and deletes Twilio's copy.
5. `voice_check_report`: Whisper transcribes Rudi's channel **without being told the language**
   and names the language it hears; a model compares it with what Rudi was given →
   `calls/{id}/voice_check.json`. Rudi's side is saved under `_voice_samples/voice-check/`.

The plan: both production voices, then auditioned ElevenLabs voices in both languages, Google
Wavenet-C and Amazon Lisa; `--all` adds every auditioned voice. First run (2026-11-05): 11 of 11
heard in the right language, saying the given text. The judge's accent remark is not evidence (it
reads a transcript); the language Whisper detects from the audio is.

[`rudi_test_call_platform.py`](rudi_test_call_platform.py) still places one test call to a person
(`--step N`, `--list-voices`), for a conversation by ear.

## Known gaps

- **Eligibility unverified.** Business-initiated calling needs a Twilio account in good standing;
  this path is plain PSTN so the WhatsApp 2,000-conversation tier does **not** apply, but the
  Belgian number's regulatory bundle must be approved before it can dial.
- **Groq's free tier sets the pace.** 8,000 tokens a minute per model; a call turn asks for about
  2,500 (4,300 in the learn stage, which carries the About-me context), so about three turns a
  minute stay on Groq and the rest fall to slower providers.
- **Status callbacks arrive only at the end.** The dispatcher sends `StatusCallbackEvent` as one
  space-separated value; Twilio rejects that list (warning 21626 on every call) and falls back to
  `completed` only. Terminal status and `AnsweredBy` still arrive; `ringing`/`answered` never do.
  Sending the events as separate fields fixes it, but also starts mid-call callbacks the
  answering-machine hang-up has never seen — change it with a test call.
- **No retry policy yet.** A no-answer is recorded but nothing reschedules it.
- **No EventBridge schedule yet.** The dispatcher is invoked manually; wiring a cadence is a
  small addition once the first real calls have been heard.
- **`gateway.py`, `brain.py` and `calllog.py` are now copied across three services.** That is the
  repo's per-service convention; the platform engine layer is the way out, once the default is
  flipped and `brain.py` retires.
