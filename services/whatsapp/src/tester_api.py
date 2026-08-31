"""
MEET_RUDI — meetrudi-tester-api handler (third-party tester console).

The API behind the closed tester cohort. The front-end is a set of static pages on GitHub Pages,
so this Lambda owns ALL authentication and every decision; the browser is never trusted with
anything (§0.5). It lives in the WhatsApp service because Track A drives the very same engine —
`responder.respond` → `gateway.generate` — that answers real WhatsApp messages.

The three tracks a tester exercises:
  A  chat      — types to Rudi here; same engine, own S3 prefix, nothing reaches their phone.
  B  call      — Rudi rings their registered number and holds a real conversation.
  C  whatsapp  — they message the Twilio number first, as WhatsApp requires.

Load-bearing product rules, all enforced server-side:
- **The number is hard-locked.** Rudi only ever calls or messages the number captured at
  registration. There is no path in this API that dials anything else.
- **Five calls per tester**, and only a CONNECTED call is deducted. Voicemail, no answer and
  failures cost nothing (§ tester decision log).
- **The call goal is system-set** — one of four, assigned round-robin, never shown to the tester
  and never selectable. It reaches Rudi as an input parameter.
- **Quiet hours** 21:30–06:30 Europe/Brussels are honoured here exactly as the dispatcher
  honours them, so the console refuses with an explanation instead of failing silently.
- **One call at a time**, queued. The tester-facing copy blames demand, not our capacity.
- **Sessions idle out after 10 minutes**, checked on every request, not in the browser.

Storage lives behind `tester_store.TesterStore`; chat lives behind `store.ConversationStore` on
its own prefix. Mail goes through `tester_mail`. No vendor SDK is called from the logic below
except S3/SES/Secrets via those seams and one HTTP POST to the call dispatcher.

Routes
    GET  /health
    POST /register                     {first_name,last_name,email,phone,locale,...}
    POST /set-password                 {token,password} -> {session}
    POST /login                        {email,password} -> {session}
    POST /forgot                       {email} -> always 200 (no account enumeration)
    --- session required (X-Tester-Token) ---
    GET  /me                           -> profile, tracks, call ledger, WhatsApp instructions
    POST /ack                          -> record the do's-and-don'ts acknowledgement
    POST /logout
    GET  /chat                         -> the Track A thread
    POST /chat                         {text} -> run the engine -> {reply}
    POST /call                         -> dial, queue, or an explained refusal
    GET  /call/status                  -> queue position / live call / terminal outcome
    POST /call/leave                   -> give up your place in the queue
    POST /feedback                     {track,score,text}
    --- admin (X-Tester-Token from /admin/login) ---
    POST /admin/login                  {password} -> {session}
    GET  /admin/overview               -> KPIs + settings + queue
    GET  /admin/testers                -> roster with feedback
    POST /admin/settings               {registration_open,calling_paused}
    POST /admin/action                 {action,tester_id,...}
"""

import os
import re
import json
import hmac
import base64
import datetime
import urllib.error
import urllib.request

import boto3

import store
import i18n
import responder
import personality
import gateway
import tester_store
import tester_mail
from tester_store import Tester, TesterStore

_s3 = boto3.client("s3")
_secrets = boto3.client("secretsmanager")
_ses = boto3.client("ses") if os.environ.get("TESTER_MAIL_FROM") else None

DATA_BUCKET = os.environ["DATA_BUCKET"]
CHAT_PREFIX = os.environ.get("TESTER_CHAT_PREFIX", "tester-conversations")
# Read only so it can be logged/asserted; the Function URL enforces the actual CORS allow-list.
ALLOW_ORIGIN = os.environ.get("TESTER_ALLOW_ORIGIN", "*")
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt-change-me")

ADMIN_SECRET = os.environ.get("TESTER_ADMIN_SECRET", "meetrudi/tester-console/admin")
CALL_DISPATCH_URL = os.environ.get("CALL_DISPATCH_URL", "")
CALL_DISPATCH_SECRET = os.environ.get("CALL_DISPATCH_SECRET", "meetrudi/call/dispatch-token")
CALL_PREFIX = os.environ.get("CALL_PREFIX", "calls")
CALL_MAX_SECONDS = int(os.environ.get("TESTER_CALL_MAX_SECONDS", "300"))
WA_NUMBER = os.environ.get("TESTER_WA_NUMBER", "")
WA_JOIN_PHRASE = os.environ.get("TESTER_WA_JOIN_PHRASE", "")
SUPPORT_EMAIL = os.environ.get("TESTER_SUPPORT_EMAIL", "")
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", store.DEFAULT_TZ)

STORE = TesterStore(_s3, DATA_BUCKET)
CHAT = store.ConversationStore(_s3, DATA_BUCKET, prefix=CHAT_PREFIX)
_cache: dict = {}

# The four goals map onto the phases today's brain understands. `call_goal` is passed through
# untouched as well, so the moment the four-goal brain lands the dispatcher gets it verbatim
# and this mapping stops being consulted.
GOAL_START_PHASE = {
    "GET_TO_KNOW": "learn",
    "SET_NEARTERM_GOAL": "goal",
    "GOAL_FOLLOWUP": "goal",
    "REINSTATE_TALK": "learn",
}
CALL_LANGUAGE = {"nl-BE": os.environ.get("TESTER_CALL_LANG_NL", "nl-BE"),
                 "en": os.environ.get("TESTER_CALL_LANG_EN", "en-GB")}

# Twilio's answering-machine verdicts. Rudi never talks to a machine, and an attempt that met
# one is not deducted from the tester's five.
MACHINE_ANSWERS = {"machine_start", "machine_end_beep", "machine_end_silence",
                   "machine_end_other", "fax"}
DEAD_STATUSES = {"no-answer", "busy", "failed", "canceled"}


# --------------------------------------------------------------------------- http plumbing
def _headers() -> dict:
    """Content type only — CORS belongs to the Function URL, not to us.

    The Function URL's own Cors block already emits Access-Control-Allow-Origin. Emitting it here
    as well produced `Access-Control-Allow-Origin: https://meet-rudi.github.io,
    https://meet-rudi.github.io`, which every browser rejects outright ("contains multiple
    values"), while curl and Invoke-WebRequest happily join the duplicates and show a header that
    looks perfectly correct. One owner, in the template.
    """
    return {"Content-Type": "application/json"}


def _resp(status, obj):
    return {"statusCode": status, "headers": _headers(),
            "body": json.dumps(obj, ensure_ascii=False)}


def _method(event):
    return (event.get("requestContext", {}).get("http", {}).get("method")
            or event.get("httpMethod") or "GET").upper()


def _path(event):
    return event.get("rawPath") or event.get("path") or "/"


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw) > 40000:                      # the endpoint is public; cap the payload
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _header(event, name):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return headers.get(name.lower(), "")


def _secret(secret_id):
    """Missing secrets return {} so callers fail CLOSED with a clear 503 rather than a blank 500."""
    if secret_id in _cache:
        return _cache[secret_id]
    try:
        raw = _secrets.get_secret_value(SecretId=secret_id).get("SecretString", "") or ""
    except Exception as e:  # noqa: BLE001
        print("WARN secret %s unavailable (%s)" % (secret_id, type(e).__name__))
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = {"password": raw}
    _cache[secret_id] = value
    return value


# --------------------------------------------------------------------------- validation
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^(?:\+32|0)4[5-9][0-9]{7}$")
_PASSWORD_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$")
_NAME_ALLOWED_RE = re.compile(r"^[^\W\d_][^\d_]*$", re.UNICODE)
_VOWEL_RE = re.compile(r"[aeiouyàáâäèéêëìíîïòóôöùúûüAEIOUY]")
_RUN_RE = re.compile(r"(.)\1{2,}", re.UNICODE)


def normalize_phone(raw):
    """Belgian mobile → E.164, or "" if it isn't one. Landlines and foreign numbers are refused:
    the whole test rides on Rudi calling and WhatsApping a Belgian mobile."""
    digits = re.sub(r"[\s.\-/()]", "", raw or "")
    if not _PHONE_RE.match(digits):
        return ""
    return "+32" + digits[1:] if digits.startswith("0") else digits


def name_is_plausible(value):
    """Reject keyboard mash without rejecting real Belgian names.

    Letters, spaces, hyphens and apostrophes only; at least two characters; must contain a vowel;
    no character repeated three times running. That clears "Marieke", "D'Hondt" and
    "Van der Meer" while stopping "aaaaaaaa" — which is the actual failure we saw in review.
    """
    v = (value or "").strip()
    if not 2 <= len(v) <= 60:
        return False
    if not _NAME_ALLOWED_RE.match(v.replace(" ", "").replace("-", "").replace("'", "")):
        return False
    if not _VOWEL_RE.search(v):
        return False
    return not _RUN_RE.search(v)


def _clean(value, limit):
    return (str(value or "").strip())[:limit]


# --------------------------------------------------------------------------- auth
def _session(event):
    """(session, tester, error_response). Idle expiry is enforced on every single request."""
    token = _header(event, "x-tester-token")
    if not token:
        return None, None, _resp(401, {"error": "unauthorized"})
    sess, reason = STORE.touch_session(token)
    if sess is None:
        return None, None, _resp(401, {"error": reason or "unauthorized"})
    if sess.get("role") == "admin":
        return sess, None, None
    tester = STORE.get(sess.get("tester_id") or "")
    if tester is None or tester.status == "revoked":
        STORE.close_session(token)
        return None, None, _resp(403, {"error": "revoked"})
    return sess, tester, None


def _require_admin(event):
    sess, _, err = _session(event)
    if err:
        return None, err
    if (sess or {}).get("role") != "admin":
        return None, _resp(403, {"error": "forbidden"})
    return sess, None


# --------------------------------------------------------------------------- registration
def _register(payload):
    settings = STORE.settings()
    if not settings.get("registration_open", True):
        return _resp(403, {"error": "registration_closed"})

    if _clean(payload.get("website"), 100):    # honeypot: humans never see this field
        print("TESTER register rejected: honeypot")
        return _resp(400, {"error": "invalid"})

    first = _clean(payload.get("first_name"), 60)
    last = _clean(payload.get("last_name"), 60)
    email = _clean(payload.get("email"), 254).lower()
    phone = normalize_phone(payload.get("phone"))
    locale = payload.get("locale") if payload.get("locale") in tester_store.LOCALES \
        else tester_store.DEFAULT_LOCALE

    errors = {}
    if not name_is_plausible(first):
        errors["first_name"] = "implausible"
    if not name_is_plausible(last):
        errors["last_name"] = "implausible"
    if not _EMAIL_RE.match(email):
        errors["email"] = "invalid"
    if not phone:
        errors["phone"] = "not_belgian_mobile"
    if not payload.get("consent_health"):
        errors["consent_health"] = "required"
    if not payload.get("consent_recording"):
        errors["consent_recording"] = "required"
    if errors:
        return _resp(400, {"error": "validation_failed", "fields": errors})

    tid = tester_store.tester_id(email, SALT)
    existing = STORE.get(tid)
    if existing and existing.status == "revoked":
        return _resp(403, {"error": "revoked"})

    # Identity is the email address, and a record holds exactly ONE phone number. Two people
    # sharing a mailbox must therefore not be allowed to collide, in either direction:
    #
    #   same email, different number  — the second sign-up would overwrite the first person's
    #     number on a still-pending record, so Rudi would ring one person to talk about the
    #     other's health goal. That is a disclosure of special-category data between two people,
    #     not merely a mix-up.
    #   same number, different email  — two accounts pointing at one phone. Both would call and
    #     message it, and Track C is keyed by the phone's pseudonym, so their WhatsApp threads
    #     would land on top of each other.
    #
    # Neither is resolvable from what the form collects, so we refuse and hand it to the test
    # team, who can correct a mistyped number from the admin pane. Refusing costs one person a
    # detour; guessing costs somebody their privacy.
    if existing and existing.phone and existing.phone != phone:
        print("TESTER register refused: email on record with a different number")
        return _resp(409, {"error": "email_taken_different_number"})
    clash = STORE.find_by_phone(phone, exclude=tid)
    if clash is not None:
        print("TESTER register refused: number already on another tester")
        return _resp(409, {"error": "number_already_registered"})

    if existing and existing.status in ("active", "locked"):
        # Already through the door, same number. Don't confirm or deny anything useful about the
        # account; point at login.
        return _resp(200, {"ok": True, "already_registered": True})

    help_areas = [_clean(a, 60) for a in (payload.get("help_areas") or [])][:2]
    goal = _clean(payload.get("goal"), 400) or tester_store.DEFAULT_GOAL

    tester = existing or Tester(tester_id=tid)
    tester.first_name, tester.last_name, tester.email, tester.phone = first, last, email, phone
    tester.locale, tester.help_areas, tester.goal = locale, help_areas, goal
    tester.consent_health = bool(payload.get("consent_health"))
    tester.consent_recording = bool(payload.get("consent_recording"))
    tester.whatsapp_confirmed = bool(payload.get("whatsapp_confirmed"))
    tester.wa_user_id = store.user_id(phone, SALT)
    tester.status = "pending"
    if not existing:
        tester.call_goal = tester_store.call_goal_for(STORE.count())
    STORE.put(tester)

    STORE.revoke_links(tid, "verify")          # a resubmission invalidates the earlier link
    token = STORE.issue_link(tid, "verify")
    sent = tester_mail.send_verification(_ses, email, first, token,
                                         locale=locale, support=SUPPORT_EMAIL)
    print("TESTER registered tid=%s locale=%s goal=%s mail=%s"
          % (tid, locale, tester.call_goal, sent))
    return _resp(201, {"ok": True, "mail_sent": sent})


def _set_password(payload):
    """Serves both the first-time verification and a password reset — same shape, same rules."""
    token = _clean(payload.get("token"), 200)
    password = payload.get("password") or ""
    kind = "reset" if payload.get("mode") == "reset" else "verify"
    if not _PASSWORD_RE.match(password):
        return _resp(400, {"error": "weak_password"})

    tid, reason = STORE.consume_link(token, kind)
    if not tid:
        # Log the failure (kind + reason only, never the token — a logged token is a live
        # credential). Without this line an "invalid link" report is undiagnosable after the fact,
        # which is exactly the hole we fell into the first time it happened.
        print("TESTER link refused kind=%s reason=%s" % (kind, reason or "invalid"))
        return _resp(400, {"error": reason or "invalid"})
    tester = STORE.get(tid)
    if tester is None:
        return _resp(400, {"error": "invalid"})
    if tester.status == "revoked":
        return _resp(403, {"error": "revoked"})

    tester.password_hash = tester_store.hash_password(password)
    tester.status = "active"
    tester.failed_logins, tester.locked_at = 0, ""
    if not tester.verified_at:
        tester.verified_at = store.iso_now()
    tester.last_login_at = store.iso_now()
    STORE.put(tester)
    STORE.kill_sessions(tid)                   # a password change ends every older session
    session = STORE.open_session(tid)
    print("TESTER password set tid=%s kind=%s" % (tid, kind))
    return _resp(200, {"session": session, "me": tester.public()})


def _login(payload):
    email = _clean(payload.get("email"), 254).lower()
    password = payload.get("password") or ""
    if not (_EMAIL_RE.match(email) and password):
        return _resp(401, {"error": "invalid_credentials"})

    tester = STORE.get(tester_store.tester_id(email, SALT))
    # An unknown email and a wrong password answer identically, so the endpoint can't be used to
    # discover who is in the cohort.
    if tester is None or not tester.password_hash:
        return _resp(401, {"error": "invalid_credentials"})
    if tester.status == "revoked":
        return _resp(403, {"error": "revoked"})
    if tester.locked_at:
        return _resp(403, {"error": "account_locked"})

    if not tester_store.verify_password(password, tester.password_hash):
        tester.failed_logins = int(tester.failed_logins) + 1
        if tester.failed_logins >= tester_store.MAX_FAILED_LOGINS:
            tester.locked_at = store.iso_now()
            tester.status = "locked"
        STORE.put(tester)
        if tester.locked_at:
            print("TESTER login LOCKED tid=%s" % tester.tester_id)
            return _resp(403, {"error": "account_locked"})
        return _resp(401, {"error": "invalid_credentials",
                           "attempts_left": max(0, tester_store.MAX_FAILED_LOGINS
                                                - tester.failed_logins)})

    tester.failed_logins, tester.last_login_at = 0, store.iso_now()
    STORE.put(tester)
    return _resp(200, {"session": STORE.open_session(tester.tester_id), "me": tester.public()})


def _forgot(payload):
    """Always 200. Whether an address is in the cohort is not something this endpoint reveals."""
    email = _clean(payload.get("email"), 254).lower()
    if _EMAIL_RE.match(email):
        tester = STORE.get(tester_store.tester_id(email, SALT))
        if tester and tester.status in ("active", "locked"):
            STORE.revoke_links(tester.tester_id, "reset")
            token = STORE.issue_link(tester.tester_id, "reset")
            tester_mail.send_reset(_ses, tester.email, tester.first_name, token,
                                   locale=tester.locale, support=SUPPORT_EMAIL)
    return _resp(200, {"ok": True})


# --------------------------------------------------------------------------- tester surface
def _me(sess, tester):
    settings = STORE.settings()
    return _resp(200, {
        "me": tester.public(),
        "acked": bool(sess.get("acked_at")),
        "session_idle_minutes": tester_store.SESSION_IDLE_MINUTES,
        "whatsapp": {"number": WA_NUMBER, "join_phrase": WA_JOIN_PHRASE},
        "support_email": SUPPORT_EMAIL,
        "calling_paused": bool(settings.get("calling_paused")),
        "quiet_hours": {"start": store.QUIET_START, "end": store.QUIET_END,
                        "quiet_now": _is_quiet_now()},
        "feedback": STORE.get_feedback(tester.tester_id),
    })


def _ack(event, sess, tester):
    STORE.ack_session(_header(event, "x-tester-token"))
    tester.last_ack_at = store.iso_now()
    STORE.put(tester)
    return _resp(200, {"ok": True})


def _track(tester, name, value):
    """Advance a track's progress marker, never backwards."""
    order = {"not_started": 0, "in_progress": 1, "done": 2}
    attr = "track_%s" % name
    if order.get(value, 0) > order.get(getattr(tester, attr, "not_started"), 0):
        setattr(tester, attr, value)
        STORE.put(tester)


# --------------------------------------------------------------------------- track A: chat
def _chat_thread(tester):
    meta = CHAT.get_meta(tester.tester_id)
    if meta is None:
        # First open: seed Rudi's greeting so the tester lands on a started conversation, exactly
        # as a fresh WhatsApp contact would. keep_warm=False — nothing proactive is ever
        # scheduled for a tester's console thread.
        meta = store.ContactMeta(user_id=tester.tester_id, display_name=tester.first_name,
                                 locale=tester.locale, keep_warm=False, consent_state="granted")
        CHAT.put_meta(meta)
        reply, ai_state, _ = responder.respond({}, "", locale=_engine_locale(tester.locale))
        CHAT.record_outbound(tester.tester_id,
                             store.Message(id=store.new_message_id(), direction="out",
                                           type="text", text=reply, operator_id="ai:rudi"),
                             ai_state=ai_state)
    return [m.to_dict() for m in CHAT.list_messages(tester.tester_id)]


def _engine_locale(locale):
    """The engine speaks i18n locales ("nl"/"en"); the cohort is labelled nl-BE/en."""
    return "nl" if str(locale).startswith("nl") else "en"


def _chat_send(tester, payload):
    text = _clean(payload.get("text"), 2000)
    if not text:
        return _resp(400, {"error": "empty_message"})
    _chat_thread(tester)                        # guarantees meta + greeting exist
    meta = CHAT.get_meta(tester.tester_id)
    locale = meta.locale or _engine_locale(tester.locale)

    # Build the inbound now so it sorts before the reply, but persist NOTHING until we actually
    # have an answer — a rate-limited turn must not leave a question with no reply behind it.
    in_msg = store.Message(id=store.new_message_id(), direction="in", type="text", text=text)
    try:
        block = personality.resolve_block(meta.persona)
        reply, new_state, info = responder.respond(meta.ai_state, text, locale=locale,
                                                   personality_block=block)
    except gateway.AllRateLimited:
        return _resp(503, {"error": "rate_limited"})
    except Exception as e:  # noqa: BLE001 - surface engine failure honestly, don't 500 blankly
        print("ERROR tester-chat tid=%s: %s" % (tester.tester_id, type(e).__name__))
        return _resp(502, {"error": "engine_failed"})

    out = store.Message(id=store.new_message_id(), direction="out", type="text",
                        text=reply, operator_id="ai:rudi")
    CHAT.record_inbound(tester.tester_id, "", in_msg)      # phone stays empty — no PII in chat
    CHAT.record_outbound(tester.tester_id, out, ai_state=new_state,
                         locale=info.get("lang") or locale)
    _track(tester, "chat", "in_progress")
    return _resp(200, {"reply": reply, "messages": [in_msg.to_dict(), out.to_dict()]})


# --------------------------------------------------------------------------- track B: call
def _is_quiet_now(now=None):
    return store.is_quiet(now or store.now_dt(), store._tz(DEFAULT_TZ))


def _next_morning_slot(now=None):
    """First moment calling reopens after quiet hours — i.e. today or tomorrow at QUIET_END."""
    now = now or store.now_dt()
    return store.to_iso(store.next_social_start(now, store._tz(DEFAULT_TZ)))


def _tomorrow_slot(now=None):
    """Tomorrow morning's opening, used when the day's quota is gone.

    Distinct from `_next_morning_slot`: mid-afternoon that one correctly returns "now" (calling
    is open), which would be a nonsense answer to "when can I have my call?".
    """
    now = now or store.now_dt()
    tz = store._tz(DEFAULT_TZ)
    local = now.astimezone(tz)
    end = store._hhmm(store.QUIET_END)
    day = local.date() if local.time() < end else local.date() + datetime.timedelta(days=1)
    return store.to_iso(datetime.datetime.combine(day, end, tzinfo=tz))


def _call_manifest(call_id):
    try:
        raw = _s3.get_object(Bucket=DATA_BUCKET,
                             Key="%s/%s/manifest.json" % (CALL_PREFIX, call_id))["Body"].read()
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 - not written yet, or gone
        return None


def _outcome_of(manifest):
    """(outcome, finished). Only "connected" ever costs the tester one of their five."""
    if not manifest:
        return "", False
    tel = manifest.get("telephony") or {}
    answered_by = str(tel.get("answered_by") or "")
    call_status = str(tel.get("call_status") or "")
    if answered_by in MACHINE_ANSWERS:
        return "voicemail", True
    if call_status in DEAD_STATUSES:
        return ("no_answer" if call_status in ("no-answer", "busy") else "failed"), True
    if manifest.get("status") == "completed" or call_status == "completed":
        turns = int((manifest.get("totals") or {}).get("turns") or 0)
        return ("connected" if turns > 0 else "no_answer"), True
    if tel.get("error"):
        return "failed", True
    return "in_progress", False


def _dispatch(tester, call_id_holder):
    """POST to the call dispatcher. Returns (ok, payload_or_reason).

    The dispatcher owns the final say on whether to dial — consent, active status, quiet hours,
    destination. We re-check the same gates first so the tester gets a useful message instead of
    an opaque "skipped", but we never bypass it.
    """
    if not CALL_DISPATCH_URL:
        return False, "dispatch_not_configured"
    token = (_secret(CALL_DISPATCH_SECRET) or {})
    token = token.get("token") or token.get("password") or ""
    if not token:
        return False, "dispatch_not_configured"

    config = {
        "to": tester.phone,                     # hard-locked: the registered number, always
        "user_name": tester.first_name,
        "language": CALL_LANGUAGE.get(tester.locale, "en-GB"),
        "timezone": DEFAULT_TZ,
        "consent_state": "granted" if tester.consent_health else "unknown",
        "status": "active",
        "topic": tester.goal,
        "call_goal": tester.call_goal,          # the four-goal brain reads this verbatim
        "start_phase": GOAL_START_PHASE.get(tester.call_goal, "goal"),
        "max_seconds": CALL_MAX_SECONDS,
        "machine_detection": True,              # Rudi never talks to an answering machine
        "recording_notice": True,
        "tester_id": tester.tester_id,
    }
    body = json.dumps({"token": token, "config": config}).encode("utf-8")
    req = urllib.request.Request(CALL_DISPATCH_URL, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        print("ERROR tester-call dispatch HTTP %s" % e.code)
        return False, "dispatch_failed"
    except Exception as e:  # noqa: BLE001
        print("ERROR tester-call dispatch %s" % type(e).__name__)
        return False, "dispatch_failed"

    if not payload.get("ok"):
        return False, payload.get("skipped") or payload.get("error") or "dispatch_failed"
    call_id_holder["call_id"] = payload.get("call_id") or ""
    return True, payload


def _call_request(tester):
    settings = STORE.settings()
    if settings.get("calling_paused"):
        return _resp(200, {"state": "paused"})
    if tester.calls_left() <= 0:
        return _resp(200, {"state": "calls_spent", "calls_used": tester.calls_used,
                           "calls_max": tester.calls_max})
    if _is_quiet_now():
        return _resp(200, {"state": "quiet_hours", "reopens_at": _next_morning_slot(),
                           "quiet_start": store.QUIET_START, "quiet_end": store.QUIET_END})
    cap = int(settings.get("daily_call_cap") or tester_store.DAILY_CALL_CAP)
    if STORE.calls_today() >= cap:
        # Deliberately worded as Rudi's testing quota being consumed by testers — never as our
        # capacity running out.
        return _resp(200, {"state": "quota_spent", "next_slot": _tomorrow_slot()})

    q = STORE.enqueue(tester.tester_id)
    pos = STORE.position(tester.tester_id)
    if pos > 0:
        return _resp(200, {"state": "queued", "position": pos,
                           "waiting": len(q.get("waiting", []))})

    holder = {}
    ok, result = _dispatch(tester, holder)
    if not ok:
        STORE.dequeue(tester.tester_id)
        reason = result if isinstance(result, str) else "dispatch_failed"
        if reason == "quiet-hours":
            return _resp(200, {"state": "quiet_hours", "reopens_at": _next_morning_slot()})
        return _resp(503, {"state": "failed", "error": reason})

    STORE.set_active_call(tester.tester_id, holder.get("call_id", ""))
    STORE.bump_calls_today()
    tester.last_call_at, tester.last_call_outcome = store.iso_now(), "in_progress"
    STORE.put(tester)
    _track(tester, "call", "in_progress")
    print("TESTER call placed tid=%s goal=%s call=%s"
          % (tester.tester_id, tester.call_goal, holder.get("call_id", "")))
    return _resp(200, {"state": "dialing", "call_id": holder.get("call_id", "")})


def _call_status(tester):
    """Poll target for Track B. Settles the ledger the moment a call reaches a terminal state."""
    pos = STORE.position(tester.tester_id)
    if pos > 0:
        return _resp(200, {"state": "queued", "position": pos})
    if pos < 0:
        return _resp(200, {"state": "idle", "calls_left": tester.calls_left(),
                           "last_outcome": tester.last_call_outcome})

    q = STORE.queue()
    call_id = (q.get("active") or {}).get("call_id") or ""
    if not call_id:
        return _resp(200, {"state": "dialing"})

    outcome, finished = _outcome_of(_call_manifest(call_id))
    if not finished:
        return _resp(200, {"state": "on_call", "call_id": call_id})

    # Terminal. Deduct ONLY a connected call, then free the line for whoever is next.
    if tester.last_call_outcome != outcome:
        if outcome == "connected":
            tester.calls_used = min(tester.calls_max, tester.calls_used + 1)
            _track(tester, "call", "done")
        tester.last_call_outcome = outcome
        STORE.put(tester)
    STORE.dequeue(tester.tester_id)
    print("TESTER call finished tid=%s outcome=%s used=%d"
          % (tester.tester_id, outcome, tester.calls_used))
    return _resp(200, {"state": "finished", "outcome": outcome,
                       "calls_used": tester.calls_used, "calls_left": tester.calls_left()})


# --------------------------------------------------------------------------- feedback
def _feedback(tester, payload):
    track = _clean(payload.get("track"), 20)
    if track not in tester_store.TRACKS:
        return _resp(400, {"error": "unknown_track"})
    text = _clean(payload.get("text"), 1000)
    score = payload.get("score")
    if score is not None:
        try:
            score = int(score)
        except (ValueError, TypeError):
            return _resp(400, {"error": "bad_score"})
        if not 0 <= score <= 10:
            return _resp(400, {"error": "bad_score"})
    if score is None and not text:
        return _resp(400, {"error": "empty_feedback"})

    rec = STORE.save_feedback(tester.tester_id, track, score, text)
    print("TESTER feedback tid=%s track=%s score=%s len=%d"
          % (tester.tester_id, track, score, len(text)))
    return _resp(200, {"ok": True, "sent_at": rec["sent_at"]})


# --------------------------------------------------------------------------- admin
def _admin_login(payload):
    creds = _secret(ADMIN_SECRET)
    expected = (creds or {}).get("password") or ""
    if not expected:
        return _resp(503, {"error": "admin_not_configured"})
    if not hmac.compare_digest(str(payload.get("password") or ""), expected):
        print("TESTER admin login refused")
        return _resp(401, {"error": "invalid_credentials"})
    return _resp(200, {"session": STORE.open_session("", role="admin")})


def _admin_overview():
    testers = STORE.list_all()
    settings = STORE.settings()
    q = STORE.queue()
    return _resp(200, {
        "kpis": {
            "registered": len(testers),
            "unverified": sum(1 for t in testers if t.status == "pending"),
            "active_sessions": STORE.active_sessions(),
            "calls_today": STORE.calls_today(),
            "daily_call_cap": int(settings.get("daily_call_cap")
                                  or tester_store.DAILY_CALL_CAP),
            "queued": len(q.get("waiting", [])),
            "feedback_in": STORE.feedback_count(),
            "feedback_possible": len(testers) * len(tester_store.TRACKS),
        },
        "settings": settings,
        "queue": q,
        "quiet_now": _is_quiet_now(),
    })


def _admin_testers():
    rows = []
    for t in STORE.list_all():
        row = t.admin_row()
        row["feedback"] = STORE.get_feedback(t.tester_id)
        row["queue_position"] = STORE.position(t.tester_id)
        rows.append(row)
    return _resp(200, {"testers": rows})


def _admin_settings(payload):
    patch = {}
    for key in ("registration_open", "calling_paused"):
        if key in payload:
            patch[key] = bool(payload[key])
    for key in ("daily_call_cap", "invited_count"):
        if key in payload:
            try:
                patch[key] = max(0, int(payload[key]))
            except (ValueError, TypeError):
                return _resp(400, {"error": "bad_value", "field": key})
    if not patch:
        return _resp(400, {"error": "nothing_to_change"})
    print("TESTER admin settings %s" % json.dumps(patch))
    return _resp(200, {"settings": STORE.save_settings(patch)})


def _admin_action(payload):
    """One switch for every roster action. Erasure is deliberately absent — phase 2."""
    action = _clean(payload.get("action"), 40)
    tid = _clean(payload.get("tester_id"), 64)
    tester = STORE.get(tid)
    if tester is None:
        return _resp(404, {"error": "unknown_tester"})

    if action == "resend_verification":
        STORE.revoke_links(tid, "verify")
        token = STORE.issue_link(tid, "verify")
        sent = tester_mail.send_verification(_ses, tester.email, tester.first_name, token,
                                             locale=tester.locale, support=SUPPORT_EMAIL)
        result = {"mail_sent": sent}
    elif action == "send_reset":
        STORE.revoke_links(tid, "reset")
        token = STORE.issue_link(tid, "reset")
        sent = tester_mail.send_reset(_ses, tester.email, tester.first_name, token,
                                      locale=tester.locale, support=SUPPORT_EMAIL)
        result = {"mail_sent": sent}
    elif action == "force_verify":
        tester.verified_at = tester.verified_at or store.iso_now()
        result = {"verified_at": tester.verified_at}
    elif action == "unlock":
        tester.locked_at, tester.failed_logins = "", 0
        tester.status = "active" if tester.password_hash else "pending"
        result = {"status": tester.status}
    elif action == "reset_call_limit":
        tester.calls_used = 0
        result = {"calls_left": tester.calls_left()}
    elif action == "grant_calls":
        try:
            extra = max(1, min(20, int(payload.get("calls") or 5)))
        except (ValueError, TypeError):
            return _resp(400, {"error": "bad_value"})
        tester.calls_max += extra
        result = {"calls_max": tester.calls_max}
    elif action == "kill_session":
        result = {"sessions_dropped": STORE.kill_sessions(tid)}
    elif action == "revoke":
        tester.status = "revoked"
        STORE.kill_sessions(tid)
        STORE.revoke_links(tid)
        STORE.dequeue(tid)
        result = {"status": tester.status}
    elif action == "reinstate":
        tester.status = "active" if tester.password_hash else "pending"
        result = {"status": tester.status}
    elif action == "set_phone":
        # Registration refuses a number that conflicts with an existing record, so a tester who
        # mistyped theirs cannot fix it alone. This is that escape hatch — and the only way a
        # number ever changes after sign-up.
        new_phone = normalize_phone(payload.get("phone"))
        if not new_phone:
            return _resp(400, {"error": "not_belgian_mobile"})
        holder = STORE.find_by_phone(new_phone, exclude=tid)
        if holder is not None:
            return _resp(409, {"error": "number_already_registered"})
        tester.phone = new_phone
        tester.wa_user_id = store.user_id(new_phone, SALT)   # Track C follows the number
        result = {"phone_masked": tester_store.mask_phone(new_phone)}
    elif action == "set_call_goal":
        goal = _clean(payload.get("call_goal"), 40)
        if goal not in tester_store.CALL_GOALS:
            return _resp(400, {"error": "unknown_call_goal"})
        tester.call_goal = goal
        result = {"call_goal": goal}
    elif action == "drop_from_queue":
        STORE.dequeue(tid)
        result = {"queue": STORE.queue()}
    else:
        return _resp(400, {"error": "unknown_action"})

    STORE.put(tester)
    print("TESTER admin action=%s tid=%s" % (action, tid))
    return _resp(200, {"ok": True, "action": action, "result": result,
                       "tester": tester.admin_row()})


# --------------------------------------------------------------------------- router
def handler(event, context):
    method = _method(event)
    if method == "OPTIONS":
        return _resp(200, {})

    path = _path(event).rstrip("/") or "/"
    parts = [p for p in path.split("/") if p]

    if path in ("/", "/health"):
        return _resp(200, {"ok": True, "service": "meetrudi-tester-api"})

    try:
        # ---- public
        if parts == ["register"] and method == "POST":
            return _register(_body(event))
        if parts == ["set-password"] and method == "POST":
            return _set_password(_body(event))
        if parts == ["login"] and method == "POST":
            return _login(_body(event))
        if parts == ["forgot"] and method == "POST":
            return _forgot(_body(event))
        if parts == ["admin", "login"] and method == "POST":
            return _admin_login(_body(event))

        # ---- admin
        if parts and parts[0] == "admin":
            _, err = _require_admin(event)
            if err:
                return err
            if parts == ["admin", "overview"] and method == "GET":
                return _admin_overview()
            if parts == ["admin", "testers"] and method == "GET":
                return _admin_testers()
            if parts == ["admin", "settings"] and method == "POST":
                return _admin_settings(_body(event))
            if parts == ["admin", "action"] and method == "POST":
                return _admin_action(_body(event))
            return _resp(404, {"error": "not_found"})

        # ---- tester
        sess, tester, err = _session(event)
        if err:
            return err
        if tester is None:                      # an admin session on a tester route
            return _resp(403, {"error": "forbidden"})

        if parts == ["logout"] and method == "POST":
            STORE.close_session(_header(event, "x-tester-token"))
            return _resp(200, {"ok": True})
        if parts == ["me"] and method == "GET":
            return _me(sess, tester)
        if parts == ["ack"] and method == "POST":
            return _ack(event, sess, tester)
        if parts == ["chat"]:
            if method == "GET":
                return _resp(200, {"messages": _chat_thread(tester)})
            if method == "POST":
                return _chat_send(tester, _body(event))
        if parts == ["call"] and method == "POST":
            return _call_request(tester)
        if parts == ["call", "status"] and method == "GET":
            return _call_status(tester)
        if parts == ["call", "leave"] and method == "POST":
            STORE.dequeue(tester.tester_id)
            return _resp(200, {"ok": True, "state": "idle"})
        if parts == ["feedback"] and method == "POST":
            return _feedback(tester, _body(event))

        return _resp(404, {"error": "not_found"})
    except Exception as e:  # noqa: BLE001 - never leak a stack trace to a public endpoint
        print("ERROR tester-api %s: %s" % (path, type(e).__name__))
        return _resp(500, {"error": "internal"})
