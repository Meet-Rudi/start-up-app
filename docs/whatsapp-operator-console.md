# WhatsApp Operator Console — Blueprint & Status

Operator-run 1:1 WhatsApp messaging via Twilio. A human operator reads incoming messages and
types replies from a web console; **no AI is on the send path**. Companion to
[services/whatsapp/README.md](../services/whatsapp/README.md).

## Decisions (locked)
1. **AI responder ON (toggle `AI_RESPONDER`, default true).** The processor persists inbound,
   then runs the "Real Rudi" AI responder (`responder.py`, the try-rudi `learn→goal→commit`
   experience ported server-side) and replies. Set `AI_RESPONDER=false` to revert to operator-
   console mode (persist-only; a human replies from the console). Either way every message is
   stored, so the console always observes the live conversation. AI replies are **reactive** →
   never quiet-hours-gated. Guardrails (`rudi_guardrails.md`, no medical advice) are prepended
   to every generation (§0.2/§6). *(This supersedes the original operator-only decision.)*
2. **Roster = S3-only** — no new datastore; roster is derived by listing the `conversations/`
   prefix and reading each `meta.json`. Fine at pilot scale; DynamoDB index is a later upgrade.
3. **Auth = Cognito + private S3/CloudFront** — the console is EU-hosted and auth-gated, never on
   public GitHub Pages. Interim: the console API fails CLOSED behind a shared-secret token
   (`meetrudi/whatsapp/console-token`) until Cognito lands.

## Storage (per-conversation isolation)
```
conversations/{userId}/meta.json                       # contact record + state
conversations/{userId}/messages/{ts_ms}-{msgId}.json   # one object per message (no append race)
```
`userId` = HMAC pseudonym of the phone. Phone/name (PII) live only in `meta.json` (AWS-EU
plane), never in keys or logs.

## Components
| Component | Type | Role |
|---|---|---|
| `meetrudi-wa-webhook` | Lambda (Function URL) | Twilio inbound → validate → SQS FIFO (unchanged) |
| `meetrudi-wa-processor` | Lambda (SQS) | Persist inbound via `ConversationStore`; no reply |
| `meetrudi-wa-console-api` | Lambda (Function URL) | Roster / thread / read / operator send |
| `ConversationStore` (`store.py`) | Library | S3 system-of-record + window logic |
| `console/` | Static app | Roster + WhatsApp-style thread + composer |

## Development blocks & status
| Block | Scope | Integrations | Status |
|---|---|---|---|
| 0 — Contracts | schemas, S3 layout, window rules | none | ✅ done |
| 1 — Store | `ConversationStore` over S3 + contract tests | none | ✅ done |
| 2 — Console read API | roster / thread / poll-since | none | ✅ done |
| 3 — Console UI | roster + thread + switching (DEMO data) | none | ✅ done |
| 4 — Inbound wiring | processor persists inbound, no auto-reply | Twilio in | ✅ code done |
| 5 — Outbound wiring | window check → send → persist | Twilio out | ✅ code done |
| R1 — Proactive meta fields + lifecycle | `next_proactive_at/kind`, tz, cadence, dormancy | none | ✅ done |
| R2 — Quiet-hours gate | tz/DST-aware `is_quiet` + social-hours helpers | none | ✅ done |
| R3 — Anti-drift scheduler | `compute_next_proactive` wired into record_in/outbound | none | ✅ done |
| Auth — Cognito + CloudFront | operator login, private hosting | Cognito | ⏳ next, before live PII |
| R4 — Keep-warm runner | `meetrudi-wa-reengage` Lambda + EventBridge tick; nudge (in-window) / template fallback; per-number `keep_warm` toggle in the console | Twilio | ✅ done (test mode; template needs approved SID) |
| R5 — Status callbacks | delivery ticks + failure/block detection | Twilio status | ⏳ |
| 6 — Contact records + templates | edit fields, out-of-window template send | Twilio templates | ⏳ |
| 8 — Media + scale | media→S3, DynamoDB roster index | Twilio | ⏳ |

## Proactive scheduling (24h-window bridge)
We never force a session alive — only a user reply reopens the 24h window. The compliant bridge,
all precomputed on each message event and stored on `meta`:

- **Event-time schedule.** `record_inbound/outbound` call `compute_next_proactive(meta, now)`,
  which sets `next_proactive_at` (UTC) + `next_proactive_kind` (`nudge` | `template`). The 5-min
  runner (R4) just polls `list_due(now)` — no per-run recomputation.
- **Anti-drift nudge (free, in-window).** A free-form nudge is scheduled at the **last
  social-hours slot before the window closes**. If the window would lapse during quiet hours
  (21:30–06:30 local, tz-aware/DST-aware via `zoneinfo`), the nudge is **pulled forward** into
  the prior evening so the reply re-anchors the timer into daytime. Over a few days the rhythm
  self-corrects into social-hours hops.
- **Template (paid) fallback.** Only if the nudge lapses (user silent through a social window)
  do we schedule a pre-approved template at the next morning social slot, capped by
  `MIN_TEMPLATE_GAP` (~2–3×/week) and stopped after `MAX_TEMPLATE_MISSES` (dormant → protect
  number quality).
- **Reactive ≠ proactive.** Operator replies (and the future AI auto-reply) are reactive — never
  quiet-gated, never consume the nudge. Quiet hours gate **only** proactive sends.
- **Consent-gated.** No proactive send unless `consent_state == "granted"` (§5, GDPR Art. 9).

Tunables live in `store.py`: `DEFAULT_TZ`, `QUIET_START/END`, `NUDGE_LEAD`, `MIN_TEMPLATE_GAP`,
`MAX_TEMPLATE_MISSES`. Timezone math uses stdlib `zoneinfo`; the AWS Lambda runtime provides the
system IANA tz database, so no runtime dependency is bundled. Local Windows testing needs
`pip install -r services/whatsapp/tests/requirements-dev.txt` (tzdata).

## Tests
Zero-dependency (`python -m unittest discover -s services/whatsapp/tests`): store contract,
window in/out behavior, conversation isolation, poll cursor, router, in/out-of-window send,
fail-closed auth, processor persistence. No boto3/moto/network; synthetic data only.

## Live-safety gate
Before the console API points at the live Twilio number:
1. Create `meetrudi/whatsapp/console-token` (API fails closed until then), **and/or**
2. Complete the Cognito + CloudFront auth block (replaces the token gate).
Processor→S3 persistence is in-plane and safe to deploy independently.
