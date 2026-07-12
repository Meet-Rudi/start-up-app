"""
MEET_RUDI — meetrudi-test-console-api handler (internal personality test harness).

A separate, login-gated console for MASS-TESTING Rudi personalities without WhatsApp/Twilio. It
drives the EXACT same conversation engine as production (responder.respond → gateway.generate),
with the conversation's fixed persona, but:

  - test conversations are stored under their own S3 prefix (TEST_PREFIX, default
    "test-conversations/"), completely isolated from live WhatsApp conversations. The operator
    console and the proactive re-engagement runner only ever read the default "conversations/"
    prefix, so they never see — and never message — a test conversation.
  - conversations are created with keep_warm=False (belt-and-braces: nothing proactive is ever
    scheduled for a test), and there is no outbound Twilio send — "Send" just runs the engine and
    returns Rudi's reply for the tester to read.

Auth (fixed email + password): validated server-side against a Secrets Manager secret
(meetrudi/test-console/auth = {"email","password","token"}); on success /login returns the token,
which the SPA then sends as X-Console-Token on every call. Never trust the client (§0.5 / §5).

Routes:
    GET  /health
    POST /login                               {email,password} -> {token}
    GET  /conversations                       -> active test conversations (recent-first)
    POST /conversations                       {name, persona} -> create (+ seeds Rudi's greeting)
    GET  /conversations/{uid}/messages        -> thread (chronological)
    POST /conversations/{uid}/messages        {text} -> run engine -> {reply, messages}
    POST /conversations/{uid}/delete          -> soft-delete (archive; data retained)
    GET  /conversations/{uid}/export          -> {filename, markdown} (single)
    GET  /export?scope=all                    -> {filename, markdown} (all active)
    GET  /export?scope=interval&from=&to=     -> {filename, markdown} (by launch date, inclusive)
"""

import os
import hmac
import json
import uuid
import datetime

import boto3

import store
import i18n
import responder
import personality
import gateway

_s3 = boto3.client("s3")
_secrets = boto3.client("secretsmanager")
DATA_BUCKET = os.environ["DATA_BUCKET"]
TEST_PREFIX = os.environ.get("TEST_PREFIX", "test-conversations")
ALLOW_ORIGIN = os.environ.get("CONSOLE_ALLOW_ORIGIN", "*")

# Auth: a single fixed operator credential, held in Secrets Manager (never in code/env/git).
#   TEST_AUTH_SECRET  — Secrets Manager name holding {"email","password","token"} (live posture).
#   TEST_CONSOLE_*    — DEV/tests only direct overrides (never set in a live template).
#   TEST_REQUIRE_AUTH — "true" ⇒ fail CLOSED if no credential is configured.
TEST_AUTH_SECRET = os.environ.get("TEST_AUTH_SECRET", "")
ENV_EMAIL = os.environ.get("TEST_CONSOLE_EMAIL", "")
ENV_PASSWORD = os.environ.get("TEST_CONSOLE_PASSWORD", "")
ENV_TOKEN = os.environ.get("TEST_CONSOLE_TOKEN", "")
REQUIRE_AUTH = os.environ.get("TEST_REQUIRE_AUTH", "false").lower() == "true"

# Brute-force lockout: N consecutive failed logins for an email locks it until an admin clears it
# manually. Lock state lives in one small S3 object so the admin can unlock by editing/deleting it
# (set the email's "locked_at" to "" and "failed" to 0, or delete the whole object). A successful
# login resets the counter. Only the CONFIGURED email is tracked (a wrong email can neither succeed
# nor lock the real account, and won't bloat the state object).
LOGIN_STATE_KEY = os.environ.get("LOGIN_STATE_KEY", "test-console/login-state.json")
MAX_FAILED_LOGINS = int(os.environ.get("LOGIN_MAX_FAILED", "10"))

STORE = store.ConversationStore(_s3, DATA_BUCKET, prefix=TEST_PREFIX)
_creds_cache: dict = {}


# --------------------------------------------------------------------------- auth
def _creds():
    """{"email","password","token"} from env override or the secret, cached. {} if unconfigured."""
    if ENV_EMAIL and ENV_PASSWORD and ENV_TOKEN:
        return {"email": ENV_EMAIL, "password": ENV_PASSWORD, "token": ENV_TOKEN}
    if not TEST_AUTH_SECRET:
        return {}
    if "c" not in _creds_cache:
        try:
            raw = _secrets.get_secret_value(SecretId=TEST_AUTH_SECRET).get("SecretString", "") or "{}"
            obj = json.loads(raw)
            _creds_cache["c"] = obj if isinstance(obj, dict) else {}
        except Exception as e:  # noqa: BLE001
            print("WARN test-console creds unavailable: %s" % e)
            _creds_cache["c"] = {}
    return _creds_cache["c"]


def _authorized(event):
    creds = _creds()
    token = creds.get("token")
    if not token:
        return not REQUIRE_AUTH   # fail CLOSED when auth is required but nothing is configured
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    tok = headers.get("x-console-token") or headers.get("authorization", "").replace("Bearer ", "")
    return bool(tok) and hmac.compare_digest(tok, token)


def _load_login_state():
    try:
        raw = _s3.get_object(Bucket=DATA_BUCKET, Key=LOGIN_STATE_KEY)["Body"].read()
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 - absent/corrupt state means "no failures recorded yet"
        return {}


def _save_login_state(state):
    _s3.put_object(Bucket=DATA_BUCKET, Key=LOGIN_STATE_KEY,
                   Body=json.dumps(state, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")


def _locked_resp():
    return _resp(403, {"error": "account_locked",
                       "detail": "Too many failed logins. This account is locked until an admin unlocks it."})


def _login(payload):
    creds = _creds()
    if not (creds.get("email") and creds.get("password") and creds.get("token")):
        return _resp(503, {"error": "auth_not_configured"})
    email = (payload.get("email") or "").strip()
    password = payload.get("password") or ""
    known = bool(email) and hmac.compare_digest(email, creds["email"])

    state = _load_login_state()
    rec = state.get(email, {"failed": 0, "locked_at": ""}) if known else {"failed": 0, "locked_at": ""}

    # Already locked → refuse regardless of password; only an admin clearing S3 can undo it.
    if known and rec.get("locked_at"):
        print("TEST login blocked (locked) email=<known>")
        return _locked_resp()

    if known and hmac.compare_digest(password, creds["password"]):
        if rec.get("failed"):                      # success clears the streak
            rec["failed"] = 0
            state[email] = rec
            _save_login_state(state)
        return _resp(200, {"token": creds["token"]})

    # Failed. Only track the configured email (a wrong email can't lock the real account).
    if not known:
        return _resp(401, {"error": "invalid_credentials"})
    rec["failed"] = int(rec.get("failed", 0)) + 1
    if rec["failed"] >= MAX_FAILED_LOGINS:
        rec["locked_at"] = store.iso_now()
    state[email] = rec
    _save_login_state(state)
    if rec.get("locked_at"):
        print("TEST login now LOCKED after %d failures" % rec["failed"])
        return _locked_resp()
    return _resp(401, {"error": "invalid_credentials",
                       "attempts_left": max(0, MAX_FAILED_LOGINS - rec["failed"])})


# --------------------------------------------------------------------------- http helpers
def _resp(status, obj):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(obj, ensure_ascii=False)}


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


# --------------------------------------------------------------------------- serialization
def _row(m):
    return {
        "user_id": m.user_id,
        "name": m.display_name or m.user_id,
        "persona": m.persona,
        "persona_effective": m.persona or personality.DEFAULT_SLUG,
        "created_at": m.created_at,
        "last_message_at": m.last_message_at,
        "last_message_preview": m.last_message_preview,
        "last_direction": m.last_direction,
        "message_count": m.msg_total,
    }


# --------------------------------------------------------------------------- conversations
def _list():
    rows = [_row(m) for m in STORE.list_roster() if m.status == "active"]
    return _resp(200, {"conversations": rows})


def _create(payload):
    name = (payload.get("name") or "").strip()
    if not name:
        return _resp(400, {"error": "name_required"})
    slug = (payload.get("persona") or "").strip()
    if slug and slug not in {p["slug"] for p in personality.list_available()}:
        return _resp(400, {"error": "unknown_personality", "slug": slug})

    uid = "test_" + uuid.uuid4().hex[:20]
    meta = store.ContactMeta(user_id=uid, display_name=name, persona=slug,
                             keep_warm=False, consent_state="granted")
    STORE.put_meta(meta)

    # Seed Rudi's opening greeting (intro path — no model call, no S3), so the tester opens onto a
    # started conversation exactly like a fresh WhatsApp contact would.
    reply, ai_state, _ = responder.respond({}, "", locale=i18n.DEFAULT_LOCALE)
    out = store.Message(id=store.new_message_id(), direction="out", type="text",
                        text=reply, operator_id="ai:rudi")
    STORE.record_outbound(uid, out, ai_state=ai_state)
    print("TEST create uid=%s persona=%s" % (uid, slug or "(default)"))
    return _resp(201, {"conversation": _row(STORE.get_meta(uid))})


def _thread(uid):
    meta = STORE.get_meta(uid)
    if meta is None:
        return _resp(404, {"error": "unknown conversation"})
    return _resp(200, {"conversation": _row(meta),
                       "messages": [m.to_dict() for m in STORE.list_messages(uid)]})


def _send(uid, payload):
    text = (payload.get("text") or "").strip()
    if not text:
        return _resp(400, {"error": "empty message"})
    meta = STORE.get_meta(uid)
    if meta is None:
        return _resp(404, {"error": "unknown conversation"})

    locale = meta.locale or i18n.DEFAULT_LOCALE
    # Build the inbound now (timestamp precedes the model call, so it sorts before the reply) but
    # DON'T persist it until we actually have a reply — otherwise a rate-limited/failed turn leaves
    # an orphaned user message with no answer, and a resend would duplicate it.
    in_msg = store.Message(id=store.new_message_id(), direction="in", type="text", text=text)
    try:
        pblock = personality.resolve_block(meta.persona)
        reply, new_state, info = responder.respond(meta.ai_state, text, locale=locale,
                                                   personality_block=pblock)
    except gateway.AllRateLimited:
        return _resp(503, {"error": "rate_limited", "detail": "All models are rate-limited; retry shortly."})
    except Exception as e:  # noqa: BLE001 - surface engine failure to the tester, don't 500 silently
        print("ERROR test-send uid=%s: %s" % (uid, e))
        return _resp(502, {"error": "engine_failed"})

    new_locale = info.get("lang") or locale
    out = store.Message(id=store.new_message_id(), direction="out", type="text",
                        text=reply, operator_id="ai:rudi")
    STORE.record_inbound(uid, meta.phone or "", in_msg)   # phone empty for tests (no PII)
    STORE.record_outbound(uid, out, ai_state=new_state, locale=new_locale)
    print("TEST send uid=%s phase=%s model=%s" % (uid, info.get("phase"), info.get("model")))
    return _resp(200, {"reply": reply,
                       "messages": [in_msg.to_dict(), out.to_dict()],
                       "phase": info.get("phase")})


def _delete(uid):
    meta = STORE.archive(uid)
    if meta is None:
        return _resp(404, {"error": "unknown conversation"})
    print("TEST soft-delete uid=%s" % uid)
    return _resp(200, {"ok": True})


# --------------------------------------------------------------------------- export (Markdown)
def _md_for(meta, messages):
    """One conversation → Markdown block."""
    name = meta.display_name or meta.user_id
    persona = meta.persona or personality.DEFAULT_SLUG
    lines = [
        "# Conversation — %s" % name,
        "",
        "- **Started:** %s" % (meta.created_at or ""),
        "- **Last activity:** %s" % (meta.last_message_at or ""),
        "- **Persona:** %s" % persona,
        "- **Turns:** %d" % len(messages),
        "",
        "---",
        "",
    ]
    for m in messages:
        who = "Rudi" if m.direction == "out" else name
        stamp = (m.at or "")[:19].replace("T", " ")
        body = m.text or {"image": "[photo]", "audio": "[voice note]"}.get(m.type, "[attachment]")
        lines.append("**%s** _(%s)_: %s" % (who, stamp, body))
        lines.append("")
    return "\n".join(lines)


PAGE_BREAK = '\n\n<div style="page-break-after: always;"></div>\n\n'


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")


def _export_single(uid):
    meta = STORE.get_meta(uid)
    if meta is None:
        return _resp(404, {"error": "unknown conversation"})
    md = _md_for(meta, STORE.list_messages(uid))
    safe = "".join(c if c.isalnum() else "_" for c in (meta.display_name or uid))[:40]
    return _resp(200, {"filename": "Conversation_%s_%s.md" % (safe, _stamp()), "markdown": md})


def _in_interval(created_at, dfrom, dto):
    day = (created_at or "")[:10]
    return bool(day) and dfrom <= day <= dto


def _export_bulk(scope, dfrom, dto):
    metas = [m for m in STORE.list_roster() if m.status == "active"]
    if scope == "interval":
        metas = [m for m in metas if _in_interval(m.created_at, dfrom, dto)]
    metas.sort(key=lambda m: m.created_at or "")   # chronological by launch for a stable export
    blocks = [_md_for(m, STORE.list_messages(m.user_id)) for m in metas]
    header = "# Rudi conversation export\n\n- **Generated:** %s UTC\n- **Scope:** %s\n- **Conversations:** %d\n" % (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ("%s → %s" % (dfrom, dto)) if scope == "interval" else "all", len(blocks))
    md = header + PAGE_BREAK + PAGE_BREAK.join(blocks) if blocks else header + "\n_(no conversations)_\n"
    return _resp(200, {"filename": "Conversation_Export_%s.md" % _stamp(), "markdown": md, "count": len(blocks)})


def _valid_date(s):
    try:
        datetime.date.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


def _export(event):
    scope = _query(event, "scope", "all")
    if scope == "interval":
        dfrom, dto = _query(event, "from", ""), _query(event, "to", "")
        if not (_valid_date(dfrom) and _valid_date(dto)):
            return _resp(400, {"error": "bad_date", "detail": "from/to must be YYYY-MM-DD"})
        if dfrom > dto:
            return _resp(400, {"error": "bad_range", "detail": "from must be <= to"})
        return _export_bulk("interval", dfrom, dto)
    return _export_bulk("all", "", "")


# --------------------------------------------------------------------------- router
def handler(event, context):
    method = _method(event)
    if method == "OPTIONS":
        return _resp(200, {})

    path = _path(event).rstrip("/") or "/"
    if path in ("/health", "/"):
        return _resp(200, {"ok": True, "service": "meetrudi-test-console-api"})

    if path == "/login" and method == "POST":
        return _login(_body(event))

    if not _authorized(event):
        return _resp(401, {"error": "unauthorized"})

    try:
        parts = [p for p in path.split("/") if p]

        if parts == ["conversations"]:
            if method == "GET":
                return _list()
            if method == "POST":
                return _create(_body(event))

        if parts == ["personalities"] and method == "GET":
            return _resp(200, {"personalities": personality.list_available(),
                               "default": personality.DEFAULT_SLUG})

        if parts == ["export"] and method == "GET":
            return _export(event)

        if len(parts) >= 2 and parts[0] == "conversations":
            uid = parts[1]
            if len(parts) == 2 and method == "GET":
                return _thread(uid)
            if len(parts) == 3 and parts[2] == "messages":
                if method == "GET":
                    return _thread(uid)
                if method == "POST":
                    return _send(uid, _body(event))
            if len(parts) == 3 and parts[2] == "delete" and method == "POST":
                return _delete(uid)
            if len(parts) == 3 and parts[2] == "export" and method == "GET":
                return _export_single(uid)

        return _resp(404, {"error": "not found"})
    except Exception as e:  # noqa: BLE001
        print("ERROR test-console: %s" % e)
        return _resp(500, {"error": "internal"})
