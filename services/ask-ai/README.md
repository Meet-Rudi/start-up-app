# meetrudi-ask-ai

General skill Lambda that asks an external AI endpoint a question, using a prompt template +
optional context stored in S3, with a **cascade of providers** and **Groq as the final
fallback**. Exposed via a public **Lambda Function URL** (CORS locked to the Pages origin).

## Files
```
template.yaml              SAM: Lambda + Function URL + log group (uses shared role/bucket)
src/app.py                 handler: parse -> build prompt -> cascade -> reply -> log
src/providers.py           ProviderRegistry: per-kind call logic + Secrets Manager key fetch
seed/config/ai_endpoints.json   cascade config (uploaded to s3://<bucket>/config/)
seed/prompts/howcanihelp_prompt.md   prompt template with <<- USER INPUT ->> placeholder
seed/contexts/rudi-context.md        Rudi's identity/mission/goals (system context)
```

## Request / response (Function URL, POST JSON)
```jsonc
// request
{
  "user": "platform_00000001",                              // optional (default shown)
  "user_input": "What is Rudi's mission?",                  // MANDATORY: visitor's text
  "prompt_file": "s3://meetrudi-ai-data-<acct>/prompts/howcanihelp_prompt.md",  // MANDATORY
  "context_file": "s3://meetrudi-ai-data-<acct>/contexts/rudi-context.md"       // optional
}
// response
{ "ok": true, "reply": "<markdown>", "model": "groq-llama-3.3-70b", "user": "platform_00000001" }
```
The prompt file must contain `<<- USER INPUT ->>`, replaced with `user_input` before calling
the model. On any failure the response carries a friendly `reply`, and the error is logged.

## Logging
Every call is appended to `s3://<bucket>/external_questions/howcanihelp_questions.jsonl`:
`asked_at`, `user`, `question` (raw input), `reply` (raw answer or `"Error: ..."`), `model`,
and best-effort `meta` (ip, user_agent).

## Cascade & secrets
- Order = enabled endpoints in `config/ai_endpoints.json`, then Groq (always appended in code).
- API keys live in **Secrets Manager** under `meetrudi/ai/*` (e.g. `meetrudi/ai/groq`), read by
  the shared `meetrudi-lambda-runner` role. Store the raw key string or `{"api_key":"..."}`.

## Deploy
```cmd
python deploy.py base
python deploy.py ask-ai
```
See repo root for the full deploy sequence (incl. storing the Groq secret).
