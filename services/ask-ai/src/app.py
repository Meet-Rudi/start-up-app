"""
MEET_RUDI — meetrudi-ask-ai Lambda handler.

General skill to ask an external AI endpoint a question, using a prompt template + optional
context stored in S3, with a cascade of providers and Groq as the final fallback.

Request (POST JSON to the Function URL):
    {
      "user":         "platform_00000001",                          # optional, default below
      "user_input":   "What is Rudi's mission?",                    # MANDATORY: visitor's text
      "prompt_file":  "s3://<bucket>/prompts/howcanihelp_prompt.md",# MANDATORY: .md w/ placeholder
      "context_file": "s3://<bucket>/contexts/rudi-context.md"      # optional
    }

The prompt file must contain the placeholder  <<- USER INPUT ->>  which is replaced with
`user_input` before the model is called.

Response JSON:
    { "ok": true,  "reply": "<markdown>", "model": "<name>", "user": "..." }
    { "ok": false, "reply": "<friendly message>", "error": "...", "user": "..." }

Every call (success or failure) is appended to the questions JSONL in S3.
"""

import os
import json
import base64
import datetime

import boto3

from providers import ProviderRegistry, AIError

s3 = boto3.client("s3")
registry = ProviderRegistry()

DATA_BUCKET = os.environ["DATA_BUCKET"]
ENDPOINTS_KEY = os.environ.get("ENDPOINTS_CONFIG_KEY", "config/ai_endpoints.json")
LOG_KEY = os.environ.get("QUESTIONS_LOG_KEY", "external_questions/howcanihelp_questions.jsonl")

GROQ_FALLBACK = {
    "name": "groq-fallback",
    "kind": "groq",
    "endpoint": os.environ.get("GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions"),
    "model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "secret": os.environ.get("GROQ_SECRET", "meetrudi/ai/groq"),
    "enabled": True,
}

PLACEHOLDER = "<<- USER INPUT ->>"
DEFAULT_USER = "platform_00000001"
MAX_INPUT_CHARS = 4000
FRIENDLY_ERROR = (
    "Sorry — I'm having a little trouble reaching my brain right now. "
    "Please try again in a moment. 💬"
)


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def _parse_s3_uri(uri):
    if not uri or not uri.startswith("s3://"):
        raise ValueError("Not an s3:// URI: %r" % uri)
    without = uri[len("s3://"):]
    bucket, _, key = without.partition("/")
    if not bucket or not key:
        raise ValueError("Malformed s3 URI: %r" % uri)
    return bucket, key


def _get_s3_text(uri):
    bucket, key = _parse_s3_uri(uri)
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8")


def _load_endpoints():
    """Load the ordered provider cascade from S3. Missing/broken => empty list."""
    try:
        obj = s3.get_object(Bucket=DATA_BUCKET, Key=ENDPOINTS_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        eps = data.get("endpoints", []) if isinstance(data, dict) else []
        return [e for e in eps if e.get("enabled", True)]
    except Exception as e:  # noqa: BLE001 - any failure just means "no config"
        print("INFO: endpoints config unavailable (%s); using fallback only" % e)
        return []


def _append_log(record):
    """
    Append one JSON record as a line to the questions JSONL in S3.

    NOTE: S3 has no append, so this is read-modify-write of the whole object. Fine at demo
    volume; for high throughput switch to per-record objects or Kinesis Firehose. Logging
    failures are swallowed so they never break the user-facing path.
    """
    line = json.dumps(record, ensure_ascii=False)
    try:
        try:
            existing = s3.get_object(Bucket=DATA_BUCKET, Key=LOG_KEY)["Body"].read()
        except s3.exceptions.NoSuchKey:
            existing = b""
        body = existing + line.encode("utf-8") + b"\n"
        s3.put_object(
            Bucket=DATA_BUCKET,
            Key=LOG_KEY,
            Body=body,
            ContentType="application/x-ndjson",
        )
    except Exception as e:  # noqa: BLE001
        print("WARN: failed to append questions log: %s" % e)


def _meta_from_event(event):
    """Best-effort visitor metadata (nice-to-have)."""
    try:
        http = event.get("requestContext", {}).get("http", {})
        headers = event.get("headers", {}) or {}
        return {
            "ip": http.get("sourceIp"),
            "user_agent": headers.get("user-agent"),
        }
    except Exception:  # noqa: BLE001
        return {}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _response(payload):
    # CORS headers are added by the Function URL CORS config, not here.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def handler(event, context):
    user = DEFAULT_USER
    question = ""
    meta = _meta_from_event(event)

    try:
        # --- parse request body ---
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        params = json.loads(body) if body.strip() else {}

        user = (params.get("user") or DEFAULT_USER).strip() or DEFAULT_USER
        question = (params.get("user_input") or "").strip()
        prompt_file = (params.get("prompt_file") or "").strip()
        context_file = (params.get("context_file") or "").strip()

        # --- validate ---
        if not question:
            raise ValueError("Missing mandatory 'user_input'.")
        if not prompt_file:
            raise ValueError("Missing mandatory 'prompt_file'.")
        if len(question) > MAX_INPUT_CHARS:
            question = question[:MAX_INPUT_CHARS]

        # --- assemble the prompt ---
        prompt_template = _get_s3_text(prompt_file)
        filled_prompt = prompt_template.replace(PLACEHOLDER, question)

        messages = []
        if context_file:
            messages.append({"role": "system", "content": _get_s3_text(context_file)})
        messages.append({"role": "user", "content": filled_prompt})

        # --- cascade: configured endpoints first, Groq fallback always last ---
        cascade = _load_endpoints()
        cascade.append(GROQ_FALLBACK)

        reply = None
        used = None
        errors = []
        for ep in cascade:
            try:
                reply = registry.call(ep, messages)
                used = ep.get("name")
                break
            except Exception as e:  # noqa: BLE001 - try next provider
                errors.append("%s: %s" % (ep.get("name"), e))
                continue

        if reply is None:
            raise AIError("All endpoints failed -> " + " | ".join(errors))

        _append_log({
            "asked_at": _now_iso(),
            "user": user,
            "question": question,
            "reply": reply,
            "model": used,
            "meta": meta,
        })
        return _response({"ok": True, "reply": reply, "model": used, "user": user})

    except Exception as e:  # noqa: BLE001 - heal: friendly message, still log the error
        print("ERROR: %s" % e)
        _append_log({
            "asked_at": _now_iso(),
            "user": user,
            "question": question,
            "reply": "Error: %s" % e,
            "model": None,
            "meta": meta,
        })
        return _response({"ok": False, "reply": FRIENDLY_ERROR, "error": str(e), "user": user})
