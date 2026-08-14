# meetrudi-call — Plan A outbound calling

A real phone call: Twilio dials the patient, ConversationRelay owns speech in both directions,
and an API Gateway WebSocket carries JSON text to a per-message Lambda running the **same**
`learn / goal / commit` brain as the bench and WhatsApp.

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

31 tests drive a simulated ConversationRelay client through whole calls — the wire protocol,
every gate, the voicemail fallback, interruptions, a mid-call model failure, and the ordering
that ensures Rudi's goodbye is spoken *before* the hangup. A real phone call is an expensive,
slow and irreversible way to find a bug.

## Known gaps

- **Eligibility unverified.** Business-initiated calling needs a Twilio account in good standing;
  this path is plain PSTN so the WhatsApp 2,000-conversation tier does **not** apply, but the
  Belgian number's regulatory bundle must be approved before it can dial.
- **No retry policy yet.** A no-answer is recorded but nothing reschedules it.
- **No EventBridge schedule yet.** The dispatcher is invoked manually; wiring a cadence is a
  small addition once the first real calls have been heard.
- **`gateway.py`, `brain.py` and `calllog.py` are now copied across three services.** That is the
  repo's per-service convention, but a shared layer is overdue — the Piper build established how
  to make one.
