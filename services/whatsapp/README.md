# meetrudi-whatsapp

WhatsApp connectivity for Rudi via **Twilio** (pilot). Inbound webhook → SQS FIFO → processor →
reply. Single number/persona for the pilot.

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
                   • stateless responder (gateway)        ← state/memory = NEXT step
                   • send reply via provider ──────────────► Twilio ──► user
```

## Files
- `src/provider.py` — `TwilioProvider`: signature validation, parse/normalize, fetch_media, send_text/media/template.
- `src/webhook.py` — `meetrudi-wa-webhook`: validate + enqueue + 200 fast.
- `src/processor.py` — `meetrudi-wa-processor`: pseudonymize, respond (stateless), send, log.
- `src/gateway.py` — provider cascade (copy of the shared gateway).
- `template.yaml` — SAM: queues, two Lambdas, Function URL.

## Milestone scope
Connectivity only: receive/send text, acknowledge media. **Stateless single-turn replies** (no
memory, no 24h-window/template logic yet). Per-user state + memory + the stateful Rudi engine +
media→S3/embeddings are the next infra step. PII: raw phone stays in AWS-EU; logs/AI use the
pseudonymous `userId`.

## Prereqts (one-time, console)
1. Secret `meetrudi/whatsapp/twilio` = `{"account_sid":"AC...","auth_token":"..."}`.
2. Add the SQS statement (`infra/iam/meetrudi-lambda-runner.permissions.json`) to the role.

## Deploy
```cmd
python deploy.py whatsapp
```
Prints the **Webhook URL** → set it as the Twilio Sandbox inbound webhook (HTTP POST).

## Logging
`s3://<bucket>/external_questions/whatsapp_messages.jsonl` (`at, user(pseudonymous), type, in, out, msg_id`).

## Production swap
Change `WhatsAppFrom` (template parameter) to the approved sender; swap `provider.py` for an
EU BSP / Meta Cloud API impl behind the same functions.
