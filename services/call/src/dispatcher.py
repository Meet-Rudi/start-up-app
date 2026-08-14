"""
MEET_RUDI — meetrudi-call-dispatcher.

Places an outbound call. Everything that decides WHETHER to dial lives here; everything that
decides what to say lives in brain.py behind the WebSocket.

The gates are not optional and are checked in this order — consent first, because processing
health data without it is the one failure that is not merely embarrassing:

    consent granted        CLAUDE.md §5 — consent gates processing
    contact active         not archived, not blocked
    outside quiet hours    21:30-06:30 Europe/Brussels, the same window the WhatsApp
                           re-engagement runner already respects. A 6am call about someone's
                           diabetes is worse than no call.
    per-call cap           a bounded conversation, not an open line

Invoked by EventBridge Scheduler in production, or by its Function URL during testing.
"""

import os
import json
import base64
import datetime
import zoneinfo
import urllib.parse
import urllib.request
import urllib.error

import boto3

import brain
import calllog
import relay

_secrets = boto3.client("secretsmanager")

DATA_BUCKET = os.environ["DATA_BUCKET"]
WS_URL = os.environ["WS_URL"]                       # wss://... for ConversationRelay
STATUS_URL = os.environ.get("STATUS_URL", "")       # Twilio posts call outcome here
TWILIO_SECRET = os.environ.get("TWILIO_SECRET", "meetrudi/twilio/voice")
DISPATCH_SECRET = os.environ.get("DISPATCH_SECRET", "meetrudi/call/dispatch-token")
TWILIO_REGION = os.environ.get("TWILIO_REGION", "ie1")

QUIET_START = os.environ.get("QUIET_START", "21:30")
QUIET_END = os.environ.get("QUIET_END", "06:30")
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Europe/Brussels")

_cache = {}


def _secret(secret_id):
    """Missing secrets return {} so the caller can fail CLOSED with a clear 503.

    Letting the AWS exception escape turns "you haven't created the secret yet" into an opaque
    500 with an empty body — which is exactly the wrong signal during setup, when a missing
    secret is the most likely thing to be wrong.
    """
    if secret_id in _cache:
        return _cache[secret_id]
    try:
        raw = _secrets.get_secret_value(SecretId=secret_id).get("SecretString", "") or ""
    except Exception as e:  # noqa: BLE001
        print("WARN: secret %s unavailable (%s)" % (secret_id, type(e).__name__))
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = raw
    _cache[secret_id] = value
    return value


def _response(payload, status=200):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False)}


def _hhmm(value):
    hour, minute = value.split(":")
    return datetime.time(int(hour), int(minute))


def is_quiet(now=None, tz_name=None):
    """True inside the do-not-disturb window, which wraps midnight."""
    tz = zoneinfo.ZoneInfo(tz_name or DEFAULT_TZ)
    local = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(tz).time()
    start, end = _hhmm(QUIET_START), _hhmm(QUIET_END)
    return local >= start or local < end


def gate(config, now=None):
    """Returns None when it is acceptable to dial, else the reason it is not."""
    if str(config.get("consent_state", "unknown")) != "granted":
        return "consent-not-granted"
    if str(config.get("status", "active")) != "active":
        return "contact-not-active"
    if is_quiet(now, config.get("timezone")):
        return "quiet-hours"
    if not config.get("to"):
        return "no-destination-number"
    return None


def _twilio_post(path, form, creds):
    """Twilio REST, pinned to the regional host so the call stays in the EU plane."""
    host = "api.%s.twilio.com" % TWILIO_REGION if TWILIO_REGION else "api.twilio.com"
    url = "https://%s/2010-04-01/Accounts/%s/%s" % (host, creds["account_sid"], path)
    body = urllib.parse.urlencode(form).encode("utf-8")
    token = base64.b64encode(
        ("%s:%s" % (creds["account_sid"], creds["auth_token"])).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Basic " + token,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("Twilio %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:400]))


def _hints(config):
    """Words Deepgram is most likely to mangle — the same seed the bench gives Whisper."""
    return ", ".join([b for b in (config.get("user_name"), config.get("topic")) if b])[:1000]


def place_call(config, dry_run=False):
    """Create the call record, then ask Twilio to dial. Returns (payload, status)."""
    reason = gate(config)
    if reason:
        return {"ok": False, "skipped": reason}, 200

    call_id = calllog.new_call_id()
    state = brain.new_state(config)
    manifest = calllog.start(call_id, config, state, {"placed_by": "dispatcher"})

    twiml = relay.build_twiml(WS_URL, call_id, config.get("voice_attrs"), _hints(config))
    manifest.setdefault("telephony", {}).update({"twiml": twiml, "to": config["to"]})
    calllog._put_json(calllog._manifest_key(call_id), manifest)

    if dry_run:
        return {"ok": True, "dry_run": True, "call_id": call_id, "twiml": twiml}, 200

    creds = _secret(TWILIO_SECRET) or {}
    if not creds.get("account_sid") or not creds.get("from_number"):
        manifest.setdefault("telephony", {})["error"] = "twilio-credentials-missing"
        calllog._put_json(calllog._manifest_key(call_id), manifest)
        return {"ok": False, "error": "twilio credentials not configured (%s)" % TWILIO_SECRET,
                "call_id": call_id}, 503

    form = {
        "To": config["to"],
        "From": creds["from_number"],
        "Twiml": twiml,
        # Rings for 30s. Longer just annoys someone who is not going to answer, and Meta's
        # calling rules already punish repeated unanswered attempts.
        "Timeout": str(config.get("ring_timeout", 30)),
    }
    if STATUS_URL:
        form["StatusCallback"] = "%s?call_id=%s" % (STATUS_URL, call_id)
        form["StatusCallbackEvent"] = "initiated ringing answered completed"

    created = _twilio_post("Calls.json", form, creds)
    manifest["telephony"].update({"call_sid": created.get("sid"),
                                  "dialed_at": calllog.iso(),
                                  "twilio_status": created.get("status")})
    calllog._put_json(calllog._manifest_key(call_id), manifest)
    return {"ok": True, "call_id": call_id, "call_sid": created.get("sid"),
            "status": created.get("status")}, 200


def handler(event, context):
    """Function URL for manual dispatch; EventBridge passes the same shape directly."""
    try:
        raw = event.get("body")
        if raw:
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode("utf-8")
            params = json.loads(raw)
        else:
            params = event if isinstance(event, dict) else {}

        # The Function URL is public, so a shared token guards it. Fails closed.
        if event.get("requestContext"):
            expected = (_secret(DISPATCH_SECRET) or {}).get("token")
            if not expected:
                return _response({"ok": False, "error": "dispatch auth not configured"}, 503)
            if (params.get("token") or "") != expected:
                return _response({"ok": False, "error": "unauthorized"}, 401)

        config = params.get("config") or {}
        if not isinstance(config, dict):
            return _response({"ok": False, "error": "config must be an object"}, 400)

        payload, status = place_call(config, dry_run=bool(params.get("dry_run")))
        return _response(payload, status)

    except Exception as e:  # noqa: BLE001
        print("ERROR: dispatch failed: %s" % e)
        return _response({"ok": False, "error": str(e)}, 500)
