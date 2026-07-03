# meetrudi-whatsapp

WhatsApp **operator console** backend via **Twilio** (pilot). A human operator runs 1:1
conversations from a web console — there is **no AI on the send path**. Inbound messages are
persisted per-conversation to S3; the operator reads the thread and types replies.

```
Twilio ──POST──► meetrudi-wa-webhook (Function URL)
                   • verify X-Twilio-Signature
                   • normalize → enqueue → 200
                        │
                        ▼
                 meetrudi-wa-inbound.fifo (SQS, group = user)  ──► meetrudi-wa-dlq.fifo
                        │
                        ▼
                 meetrudi-wa-processor (SQS-triggered)
                   • pseudonymize phone → userId
                   • ConversationStore.record_inbound()   ← persist + roster state; NO reply

   Operator browser (console/) ──► meetrudi-wa-console-api (Function URL)
                   • GET  /conversations                 → roster
                   • GET  /conversations/{uid}/messages  → thread (+ ?since= poll cursor)
                   • POST /conversations/{uid}/read       → clear unread
                   • POST /conversations/{uid}/messages   → operator send
                        • window check (§3) → provider.send_text → record_outbound
```

## Files
- `src/store.py` — **ConversationStore**: the S3 storage seam. Per-conversation history
  (one object per message, no append race), contact/roster state, 24h window logic,
  pseudonymization. Injectable S3 client (boto3 at runtime, a fake in tests).
- `src/provider.py` — `TwilioProvider`: signature validation, parse/normalize, fetch_media,
  send_text/media/template. The single WhatsApp swap-point.
- `src/webhook.py` — `meetrudi-wa-webhook`: validate + enqueue + 200 fast.
- `src/processor.py` — `meetrudi-wa-processor`: pseudonymize + persist inbound (operator mode).
- `src/console_api.py` — `meetrudi-wa-console-api`: roster / thread / read / operator send.
- `src/gateway.py` — AI provider cascade. **Unused in operator mode** (kept for a future
  AI-assist/draft block; not on any live path).
- `tests/` — zero-dependency unit + contract tests (`python -m unittest`, no boto3/moto).
- `template.yaml` — SAM: queues, 3 Lambdas, Function URLs.

## Storage layout (S3, per-conversation isolation)
```
conversations/{userId}/meta.json                       # contact record + conversation state
conversations/{userId}/messages/{ts_ms}-{msgId}.json   # one object per message
```
`userId` is the HMAC pseudonym of the phone. Raw phone (PII) lives only inside `meta.json`
(AWS-EU plane) — never in S3 keys or logs (§5).

## Milestone scope
Text both directions, per-conversation S3 history, roster + WhatsApp-style thread UI, operator
send with 24h window enforcement. **Deferred:** AI draft/auto-reply, media→S3 + ASR/vision,
out-of-window template send UI, delivery-status ticks, Cognito auth, DynamoDB roster index.

## Prereqs (one-time, console)
1. `meetrudi-base` deployed (data bucket) and `meetrudi-lambda-runner` role with the S3 + SQS +
   Secrets statements (`infra/iam/meetrudi-lambda-runner.permissions.json`). No new IAM needed —
   the existing bucket-wide S3 statement already covers `conversations/*`.
2. Secret `meetrudi/whatsapp/twilio` = `{"account_sid":"AC...","auth_token":"..."}`.
3. Secret `meetrudi/whatsapp/console-token` = `{"token":"<long-random>"}` — **required**; the
   console API fails CLOSED (401) until this exists. Interim gate until Cognito/CloudFront.

## Deploy
```cmd
python deploy.py whatsapp
```
Prints the **Webhook URL** (→ Twilio inbound webhook, HTTP POST) and the **Console API URL**
(→ `console/config.js` `API_BASE`, once auth is in place).

## Console frontend
`console/` (repo root) is a static app — **not** on the public GitHub Pages site (it shows PII).
With `config.js` `API_BASE` empty it runs in **DEMO mode** on synthetic data (open `index.html`).
Hosting = private S3 + CloudFront behind Cognito (next block).

## Production swap
Change `WhatsAppFrom` to the approved sender; swap `provider.py` for an EU BSP / Meta Cloud API
impl behind the same functions. `ConversationStore` stays put (S3 layout is stable).
