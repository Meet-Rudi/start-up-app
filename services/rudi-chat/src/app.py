"""
MEET_RUDI — meetrudi-rudi-chat Lambda handler.

Stateless conversational endpoint for the "Try Rudi" page. The browser holds the state machine
(mode/phase, counters, history, the 20s restart, the ear badge); this Lambda just turns a
(phase + history + runtime state) into (reply + signals), using phase-specific system prompts.

Phases:
  - "learn"  : WhoAmI Q&A; detects intent-to-try.            signals: {want_to_try}
  - "goal"   : elicit + classify the user's goal (<=2 clarifiers, reject non-self/illegal).
               signals: {goal_status: accepted|rejected|unclear, goal_domain, goal}
  - "commit" : persuade toward one concrete commitment (<=7 turns; T2D guidance if diabetes).
               signals: {commitment_made}

Request (POST JSON to the Function URL):
  { "user": "...", "session_id": 1, "phase": "learn|goal|commit",
    "messages": [{"role":"user|assistant","content":"..."}],   # this session only
    "state": { "clarifiers_left":2, "reject_attempts_left":3,
               "attempts_left":7, "goal":"...", "goal_domain":"diabetes" } }

Response: { "ok": bool, "reply": "<markdown>", "signals": {...}, "model": "...", "phase": "..." }
"""

import os
import json
import base64
import datetime

import boto3

import gateway

s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]
LOG_KEY = os.environ.get("SESSIONS_LOG_KEY", "external_questions/tryrudi_sessions.jsonl")

# S3 keys for prompt/context assets
GUARDRAILS_KEY = "prompts/rudi_guardrails.md"
LEARN_KEY = "prompts/rudi_learn_prompt.md"
GOAL_KEY = "prompts/rudi_goal_prompt.md"
COMMIT_KEY = "prompts/rudi_commit_prompt.md"
RUDI_CONTEXT_KEY = "contexts/rudi-context.md"
HEALTH_GUIDANCE_KEY = "contexts/health-coaching-guidance.md"
# Goal domains considered health-related → inject the coaching guidance during commit.
HEALTH_DOMAINS = {"diabetes", "fitness", "diet", "sleep", "stress", "habit"}

DEFAULT_USER = "platform_00000001"
MAX_INPUT_CHARS = 4000
FRIENDLY_ERROR = "Sorry — I'm having a little trouble right now. Please try again in a moment. 💬"
TIRED_MESSAGE = (
    "😴 I'm a little tired right now after all these conversations — I need to rest for a bit. "
    "Please come back in a few hours and I'll be happy to chat again!"
)

_asset_cache = {}


def _get_s3_text(key):
    if key in _asset_cache:
        return _asset_cache[key]
    text = s3.get_object(Bucket=DATA_BUCKET, Key=key)["Body"].read().decode("utf-8")
    _asset_cache[key] = text
    return text


def _runtime_note(phase, state):
    """A short system note carrying the front-end-enforced counters/goal into the prompt."""
    if phase == "goal":
        cl = state.get("clarifiers_left", 2)
        rl = state.get("reject_attempts_left", 3)
        note = ("[Runtime: clarifying questions left = %s. If 0, you MUST now decide accept or "
                "reject — do not ask another question. Reject attempts left = %s.]" % (cl, rl))
        return note
    if phase == "commit":
        goal = state.get("goal", "(their goal)")
        al = state.get("attempts_left", 7)
        note = ("[Runtime: the user's goal is: \"%s\". Messages left to secure a commitment = %s. "
                "If that number is 1, this is your FINAL message — do NOT ask again; give the "
                "short closing (restate the action you suggest and say you'll check in later).]"
                % (goal, al))
        return note
    return ""


def _build_system(phase, state):
    if phase == "learn":
        return _get_s3_text(LEARN_KEY) + "\n\n# About me (context)\n\n" + _get_s3_text(RUDI_CONTEXT_KEY)

    guardrails = _get_s3_text(GUARDRAILS_KEY)
    if phase == "goal":
        body = _get_s3_text(GOAL_KEY)
    elif phase == "commit":
        body = _get_s3_text(COMMIT_KEY)
        if (state.get("goal_domain") or "").lower() in HEALTH_DOMAINS:
            body += "\n\n# Health & wellness coaching guidance (lifestyle support only — never medical advice)\n\n" \
                    + _get_s3_text(HEALTH_GUIDANCE_KEY)
    else:
        raise ValueError("Unknown phase: %r" % phase)

    note = _runtime_note(phase, state)
    return guardrails + "\n\n" + body + ("\n\n" + note if note else "")


def _parse_envelope(text):
    """Defensively parse the model's JSON {reply, signals}. Degrade to plain text on failure."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(text[start:end + 1])
            except (ValueError, TypeError):
                obj = None
        else:
            obj = None
    if not isinstance(obj, dict):
        return {"reply": text, "signals": {}}
    reply = obj.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = text
    signals = obj.get("signals") if isinstance(obj.get("signals"), dict) else {}
    return {"reply": reply, "signals": signals}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _meta(event):
    try:
        http = event.get("requestContext", {}).get("http", {})
        headers = event.get("headers", {}) or {}
        return {"ip": http.get("sourceIp"), "user_agent": headers.get("user-agent")}
    except Exception:  # noqa: BLE001
        return {}


def _log(record):
    line = json.dumps(record, ensure_ascii=False)
    try:
        try:
            existing = s3.get_object(Bucket=DATA_BUCKET, Key=LOG_KEY)["Body"].read()
        except s3.exceptions.NoSuchKey:
            existing = b""
        s3.put_object(Bucket=DATA_BUCKET, Key=LOG_KEY,
                      Body=existing + line.encode("utf-8") + b"\n",
                      ContentType="application/x-ndjson")
    except Exception as e:  # noqa: BLE001
        print("WARN: failed to append sessions log: %s" % e)


def _response(payload):
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False)}


def handler(event, context):
    user = DEFAULT_USER
    phase = "learn"
    session_id = None
    last_user = ""
    meta = _meta(event)
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        params = json.loads(body) if body.strip() else {}

        user = (params.get("user") or DEFAULT_USER).strip() or DEFAULT_USER
        phase = (params.get("phase") or "learn").strip()
        session_id = params.get("session_id")
        state = params.get("state") if isinstance(params.get("state"), dict) else {}
        messages = params.get("messages") if isinstance(params.get("messages"), list) else []

        # sanitize history
        clean = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = "assistant" if m.get("role") == "assistant" else "user"
            content = str(m.get("content", ""))[:MAX_INPUT_CHARS]
            if content:
                clean.append({"role": role, "content": content})
        if clean and clean[-1]["role"] == "user":
            last_user = clean[-1]["content"]
        if phase not in ("learn", "goal", "commit"):
            raise ValueError("Unknown phase: %r" % phase)
        if not clean:
            raise ValueError("No messages provided.")

        system = _build_system(phase, state)
        llm_messages = [{"role": "system", "content": system}] + clean

        result = gateway.generate(llm_messages, json_mode=True)
        env = _parse_envelope(result["text"])

        _log({"asked_at": _now_iso(), "user": user, "session_id": session_id, "phase": phase,
              "user_msg": last_user, "reply": env["reply"], "signals": env["signals"],
              "model": result["model"], "meta": meta})

        return _response({"ok": True, "reply": env["reply"], "signals": env["signals"],
                          "model": result["model"], "phase": phase})

    except gateway.AllRateLimited as e:
        _log({"asked_at": _now_iso(), "user": user, "session_id": session_id, "phase": phase,
              "user_msg": last_user, "reply": "Error: " + str(e), "signals": {}, "meta": meta})
        return _response({"ok": False, "reply": TIRED_MESSAGE, "signals": {},
                          "error": "rate_limited", "phase": phase})
    except Exception as e:  # noqa: BLE001 - heal
        print("ERROR: %s" % e)
        _log({"asked_at": _now_iso(), "user": user, "session_id": session_id, "phase": phase,
              "user_msg": last_user, "reply": "Error: " + str(e), "signals": {}, "meta": meta})
        return _response({"ok": False, "reply": FRIENDLY_ERROR, "signals": {},
                          "error": str(e), "phase": phase})
