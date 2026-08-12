# meetrudi-voice-bench — Phase 0 voice call bench

A browser page you can **talk to**, wired to the real `learn / goal / commit` engine, with every
stage timed and every call written to S3 for analysis.

This is deliberately **not** the pilot architecture. It exists to answer the questions that no
amount of design work can settle, and to answer them in days rather than weeks:

- Does the goal→commit conversation survive being **spoken** instead of typed?
- How long does the conversation actually take? (Everything downstream is priced per minute.)
- Does Rudi's wrap-up land gracefully when the time budget runs out?
- What does the **reply gap** feel like — the pause between you stopping and Rudi starting?

## What it is

```
browser (GitHub Pages)                 meetrudi-voice-bench (Lambda)
  mic ──► webm/opus ──── POST ────►  transcribe   whisper-large-v3-turbo
                                       │
                                     understand   learn/goal/commit + guardrails, from S3
                                       │          via the shared provider cascade → Groq
                                       │
  speaker ◄── wav ◄──── JSON ─────   speak        orpheus-v1-english
                                       │
                                     record       s3://…/voice-bench/calls/{call_id}/
```

Half-duplex by design: one utterance at a time, endpointed in the browser. That is enough to
measure conversation quality, and it is the cheapest possible rig — no WebSocket, no Fargate,
no telephony, no phone number.

**Everything the model produces is text before it is spoken**, so `rudi_guardrails.md` still
leads the system prompt exactly as it does in chat. The guardrail gate is intact here.

## Reusing the real brain

`brain.py` reads the **same S3 assets** the other services read — `prompts/rudi_guardrails.md`,
`rudi_goal_prompt.md`, `rudi_commit_prompt.md`, `contexts/health-coaching-guidance.md` — and
ports the phase machine from `services/whatsapp/src/responder.py` verbatim, including the
budgets (2 clarifiers, 3 rejects, 7 commit attempts). There is no second prompt corpus.

Three additions are voice-specific and live only here:

| Addition | Why |
|---|---|
| `VOICE_STYLE` | Replies must be speakable: 2-4 short sentences, no markdown, one question per turn |
| `_call_brief()` | Outbound calls know the name and topic in advance — and the **AI disclosure is mandatory** on the opening turn (EU AI Act Art. 50, in force since 2 Aug 2026) |
| `_time_note()` | As the budget runs down, the commit phase's own "FINAL message" note fires, so Rudi closes gracefully rather than being cut off |

## Call config

Sent with `action: "start"`, stored verbatim in the manifest:

```json
{
  "language": "en",
  "user_name": "Filip",
  "topic": "Getting back to daily walking after knee surgery",
  "voice": "troy",
  "start_phase": "goal",
  "max_minutes": 12,
  "store_audio": true,
  "notes": "Mentions knee pain in the evenings"
}
```

`user_name` and `topic` are also fed to Whisper as a transcription prompt, which measurably
improves proper-noun accuracy — the place ASR most often embarrasses itself.

## What lands in S3

```
voice-bench/calls/{call_id}/manifest.json          config, live state, totals, averages, outcome
voice-bench/calls/{call_id}/turns/0001.json        one object per turn — no appends, no races
voice-bench/calls/{call_id}/audio/0001-user.webm   only when store_audio is true
voice-bench/calls/{call_id}/audio/0001-rudi.wav
voice-bench/index/{YYYY-MM-DD}/{call_id}.json      light row per call, for listing a day
```

The manifest **is** the call state, so the browser holds nothing that matters and a reload
loses only the on-screen transcript.

> **Audio retention is for the bench only.** `store_audio: true` is right when the testers are
> the team; it is wrong for patients. Voice is biometric-capable and cannot be pseudonymised
> the way text can — before any real patient uses this path, set it to `false`, keep transcripts
> only, and update `docs/consent-text.md` and `gdpr-matters/records/06-dpia.md`.

## Reading the numbers

The page shows per-turn and per-call timings. The one that matters is the **reply gap** — from
you falling silent to Rudi's first sound. It decomposes as:

```
gap  =  endpoint wait (VAD silenceMs, default 900ms)
      + network up (audio upload)
      + asr_ms + llm_ms + tts_ms
      + network down
```

If the gap feels bad, look at which component owns it before changing anything. Dropping
`silenceMs` in `voice-config.js` is the cheapest win and also the one that starts cutting
people off mid-sentence — so tune it against real transcripts, not vibes.

## Deploy

Requires the `meetrudi-base` stack and the console-created `meetrudi-lambda-runner` role.
**No new IAM objects are needed** — that role already grants Logs, read/write on the data
bucket, and `GetSecretValue` on `meetrudi*`.

```cmd
python deploy.py voice-bench
```

Then paste the printed Function URL into [`site/voice-bench/voice-config.js`](../../site/voice-bench/voice-config.js)
as `window.VOICE_BENCH_API`, and push:

```cmd
git add site/voice-bench/voice-config.js ^
        services/voice-bench ^
        site/voice-bench ^
        deploy.py
git commit -m "feat(voice-bench): phase-0 spoken call bench"
git push
```

GitHub Pages then serves it at
`https://meet-rudi.github.io/start-up-app/voice-bench/`.

The mic requires HTTPS, which Pages provides. Opening `index.html` from disk will not work.

## Cost

About **€0.01–0.02 per bench call** — Whisper turbo is $0.04/audio-hour (10s minimum billed per
turn), the LLM is a few cents at most, and TTS and Lambda are noise. Run it as much as you like.

## Known limits

- **Half-duplex.** No real barge-in; the experimental toggle pauses playback on sustained mic
  energy and depends on the browser's echo cancellation. Expect false triggers on speakerphone.
- **Wideband.** Your laptop mic is far better than the 8 kHz a phone call will deliver. Rudi
  will sound worse on real telephony — this bench cannot tell you how much worse.
- **No endpointing model.** Silence-threshold VAD, not a trained turn-taking model. It will
  interrupt people who pause to think. That gap is exactly what ConversationRelay handles for
  you in Plan A, and is a real reason not to over-tune it here.
