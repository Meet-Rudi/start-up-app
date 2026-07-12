"""
MEET_RUDI — meetrudi-wa-console-api handler.

Backs the operator console: read the contact roster + a conversation thread, and send an
operator-typed reply. Function URL (behind Cognito/CloudFront in the auth block; a shared-secret
header gate here as a stopgap).

Routes:
    GET  /health
    GET  /conversations                          → roster (contacts, recent-first)
    GET  /conversations/{uid}/messages?since=KEY  → thread (chronological) + poll cursor
    POST /conversations/{uid}/read               → clear unread
    POST /conversations/{uid}/messages           → operator send { "text": "...", "operator_id": "..." }

PII discipline (§5): phone/name live server-side in meta.json and are only returned to an
authenticated operator. Logs carry the pseudonymous userId, never phone or message content.
"""

import os
import hmac
import json

import boto3

import provider
import store
import personality

_s3 = boto3.client("s3")
_secrets = boto3.client("secretsmanager")
DATA_BUCKET = os.environ["DATA_BUCKET"]
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt")
ALLOW_ORIGIN = os.environ.get("CONSOLE_ALLOW_ORIGIN", "*")

# Auth (interim, until Cognito/CloudFront — the agreed auth block):
#   CONSOLE_AUTH_TOKEN   — plaintext token, DEV/tests only (never set in a live template).
#   CONSOLE_TOKEN_SECRET — Secrets Manager name holding {"token": "..."} (live posture, §0.5).
#   CONSOLE_REQUIRE_AUTH — "true" ⇒ fail CLOSED if no token is configured (never expose PII open).
CONSOLE_AUTH_TOKEN = os.environ.get("CONSOLE_AUTH_TOKEN", "")
CONSOLE_TOKEN_SECRET = os.environ.get("CONSOLE_TOKEN_SECRET", "")
REQUIRE_AUTH = os.environ.get("CONSOLE_REQUIRE_AUTH", "false").lower() == "true"

STORE = store.ConversationStore(_s3, DATA_BUCKET)
_tok_cache: dict = {}

# NOTE: CORS is handled by the Lambda Function URL's own CORS config (template.yaml). The handler
# must NOT also emit Access-Control-* headers, or responses carry duplicate
# Access-Control-Allow-Origin values and browsers reject them.


def _console_token():
    if CONSOLE_AUTH_TOKEN:
        return CONSOLE_AUTH_TOKEN
    if not CONSOLE_TOKEN_SECRET:
        return ""
    if "t" not in _tok_cache:
        raw = _secrets.get_secret_value(SecretId=CONSOLE_TOKEN_SECRET).get("SecretString", "") or "{}"
        try:
            _tok_cache["t"] = json.loads(raw).get("token", "")
        except Exception:  # noqa: BLE001 - allow a bare-string secret
            _tok_cache["t"] = raw.strip()
    return _tok_cache["t"]

def _resp(status, obj):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(obj, ensure_ascii=False),
    }


def _authorized(event):
    token = _console_token()
    if not token:
        return not REQUIRE_AUTH  # fail CLOSED when auth is required but nothing is configured
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    tok = headers.get("x-console-token") or headers.get("authorization", "").replace("Bearer ", "")
    return hmac.compare_digest(tok, token)


# --------------------------------------------------------------------------- request parsing
def _method(event):
    return (event.get("requestContext", {}).get("http", {}).get("method")
            or event.get("httpMethod") or "GET").upper()


def _path(event):
    return event.get("rawPath") or event.get("path") or "/"


def _query(event, key, default=None):
    return (event.get("queryStringParameters") or {}).get(key, default)


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def _roster_row(m):
    return {
        "user_id": m.user_id,
        "display_name": m.display_name or m.phone,
        "phone": m.phone,
        "locale": m.locale,
        "consent_state": m.consent_state,
        "status": m.status,
        "unread_count": m.unread_count,
        "last_message_at": m.last_message_at,
        "last_message_preview": m.last_message_preview,
        "last_direction": m.last_direction,
        "in_window": m.is_in_window(),
        "window_open_until": m.window_open_until,
        "keep_warm": m.keep_warm,
        "next_proactive_at": m.next_proactive_at,
        "next_proactive_kind": m.next_proactive_kind,
        "persona": m.persona,                     # operator-chosen personality slug ("" = default)
        "persona_effective": m.persona or personality.DEFAULT_SLUG,
    }


# --------------------------------------------------------------------------- handlers
def _get_roster():
    return _resp(200, {"conversations": [_roster_row(m) for m in STORE.list_roster()]})


def _get_thread(uid, since):
    meta = STORE.get_meta(uid)
    if meta is None:
        return _resp(404, {"error": "unknown conversation"})
    msgs = STORE.list_messages(uid, since=since)
    cursor = STORE.latest_cursor(uid)
    return _resp(200, {
        "user_id": uid,
        "contact": _roster_row(meta),
        "messages": [m.to_dict() for m in msgs],
        "cursor": cursor,
    })


def _post_read(uid):
    STORE.mark_read(uid)
    return _resp(200, {"ok": True})


def _post_keepwarm(uid, payload):
    enabled = bool(payload.get("enabled", True))
    meta = STORE.set_keep_warm(uid, enabled)
    if meta is None:
        return _resp(404, {"error": "unknown conversation"})
    return _resp(200, {"ok": True, "keep_warm": meta.keep_warm,
                       "next_proactive_at": meta.next_proactive_at,
                       "next_proactive_kind": meta.next_proactive_kind})


def _get_personalities():
    """Available personas + the configured default, for the console dropdown."""
    return _resp(200, {"personalities": personality.list_available(),
                       "default": personality.DEFAULT_SLUG})


def _post_personality(uid, payload):
    """Operator sets which persona answers this conversation. "" resets to the default. The slug
    must be one of the available personalities (guard against a stale/typo'd dropdown value)."""
    slug = (payload.get("slug") or "").strip()
    if slug:
        available = {p["slug"] for p in personality.list_available()}
        if slug not in available:
            return _resp(400, {"error": "unknown_personality", "slug": slug})
    meta = STORE.set_persona(uid, slug)
    if meta is None:
        return _resp(404, {"error": "unknown conversation"})
    print("PERSONA uid=%s slug=%s" % (uid, slug or "(default)"))
    return _resp(200, {"ok": True, "persona": meta.persona,
                       "persona_effective": meta.persona or personality.DEFAULT_SLUG})


def _post_send(uid, payload):
    text = (payload.get("text") or "").strip()
    if not text:
        return _resp(400, {"error": "empty message"})

    meta = STORE.get_meta(uid)
    if meta is None or not meta.phone:
        return _resp(404, {"error": "unknown contact"})

    # §3: out-of-window → templates only (free-form is not allowed). Template send is a later block.
    if not meta.is_in_window():
        return _resp(409, {"error": "out_of_window",
                           "detail": "24h window closed; a pre-approved template is required."})

    try:
        res = provider.send_text(meta.phone, text)  # PII (phone) resolved server-side only
    except Exception as e:  # noqa: BLE001 - do not leak provider internals to the client
        print("ERROR send uid=%s: %s" % (uid, e))
        return _resp(502, {"error": "send_failed"})

    sid = (res or {}).get("sid") or store.new_message_id()
    msg = store.Message(
        id=sid, direction="out", type="text", text=text,
        twilio_sid=(res or {}).get("sid"),
        delivery_status=(res or {}).get("status", "queued"),
        operator_id=payload.get("operator_id", ""),
    )
    STORE.record_outbound(uid, msg)
    print("SENT uid=%s sid=%s" % (uid, sid))
    return _resp(201, {"message": msg.to_dict(), "cursor": STORE.latest_cursor(uid)})


# --------------------------------------------------------------------------- router
def handler(event, context):
    method = _method(event)
    if method == "OPTIONS":
        return _resp(200, {})

    path = _path(event).rstrip("/") or "/"
    if path in ("/health", "/"):
        return _resp(200, {"ok": True, "service": "meetrudi-wa-console-api"})

    if not _authorized(event):
        return _resp(401, {"error": "unauthorized"})

    try:
        parts = [p for p in path.split("/") if p]  # e.g. ["conversations", "wa_..", "messages"]

        if parts == ["conversations"] and method == "GET":
            return _get_roster()

        if parts == ["personalities"] and method == "GET":
            return _get_personalities()

        if len(parts) >= 2 and parts[0] == "conversations":
            uid = parts[1]
            if len(parts) == 3 and parts[2] == "messages":
                if method == "GET":
                    return _get_thread(uid, _query(event, "since"))
                if method == "POST":
                    return _post_send(uid, _body(event))
            if len(parts) == 3 and parts[2] == "read" and method == "POST":
                return _post_read(uid)
            if len(parts) == 3 and parts[2] == "keepwarm" and method == "POST":
                return _post_keepwarm(uid, _body(event))
            if len(parts) == 3 and parts[2] == "personality" and method == "POST":
                return _post_personality(uid, _body(event))

        return _resp(404, {"error": "not found"})
    except Exception as e:  # noqa: BLE001
        print("ERROR console-api: %s" % e)
        return _resp(500, {"error": "internal"})
