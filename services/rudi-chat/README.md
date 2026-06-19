# meetrudi-rudi-chat

Conversational endpoint powering the **Try Rudi** page (`site/try-rudi.html`). Stateless: the
browser holds the state machine; this Lambda turns *(phase + history + runtime state)* into
*(reply + signals)* using phase-specific prompts and the Groq cascade (JSON mode).

## Phases & signals
| Phase | Prompt | Returns |
|---|---|---|
| `learn` | `rudi_learn_prompt.md` + `contexts/rudi-context.md` | `{want_to_try}` |
| `goal` | `rudi_guardrails.md` + `rudi_goal_prompt.md` | `{goal_status, goal_domain, goal}` |
| `commit` | `rudi_guardrails.md` + `rudi_commit_prompt.md` (+ `diabetes-t2d-guidance.md` if `goal_domain==diabetes`) | `{commitment_made}` |

Counters (2 clarifiers, 7 commit attempts, 3 rejected-goal tries, 20s restart) are enforced in
the **front-end**; the Lambda receives them via `state` and injects a runtime note into the prompt.

## Request / response (Function URL, POST JSON)
```jsonc
// request
{ "user":"web_x", "session_id":1, "phase":"goal",
  "messages":[{"role":"user","content":"I want to lose weight"}],
  "state":{"clarifiers_left":2,"reject_attempts_left":3,"attempts_left":7,
           "goal":"lose 5kg","goal_domain":"diet"} }
// response
{ "ok":true, "reply":"<markdown>", "signals":{"goal_status":"accepted","goal_domain":"diet","goal":"lose 5kg"},
  "model":"groq-...", "phase":"goal" }
```

## Logging
Each turn → `s3://<bucket>/external_questions/tryrudi_sessions.jsonl`
(`asked_at, user, session_id, phase, user_msg, reply, signals, model, meta`).

## Reuses
Existing `meetrudi-lambda-runner` role, `meetrudi-base` data bucket, `config/ai_endpoints.json`,
`contexts/rudi-context.md`, and the `meetrudi-groq-firstkey` secret. Seeds only the new prompts
and the diabetes guidance.

## Deploy
```cmd
python deploy.py rudi-chat
```

> The T2D guidance file is a **placeholder** until the distilled official document is dropped in.
