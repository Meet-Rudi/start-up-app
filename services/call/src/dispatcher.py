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


def _dispatch_token():
    """The shared dispatch token, accepting either shape the secret may have been created in.

    The README specifies {"token": "..."}, but creating the secret as plaintext is an easy and
    common slip in the console — and `_secret()` deliberately falls back to the raw string when
    the payload is not JSON. The two behaviours met badly: a plaintext secret made the caller do
    `"abc…".get("token")`, so setup failed with an opaque 500 ('str' object has no attribute
    'get') at exactly the moment an operator is least able to diagnose it, which is the failure
    mode `_secret()` exists to prevent. Normalise here instead.
    """
    value = _secret(DISPATCH_SECRET)
    if isinstance(value, dict):
        return (value.get("token") or "").strip()
    return str(value or "").strip()


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
        # An override exists because the team tests outside working hours, and faking the
        # timezone to get past the gate would be both dishonest and invisible afterwards.
        # This way the gate still runs, the decision is deliberate, and place_call() stamps it
        # on the manifest so any call placed at night is auditable.
        if not config.get("override_quiet_hours"):
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


def _twilio_request(url, creds, form=None, method="GET"):
    """Authenticated call to any Twilio host. Dialing Permissions live on voice.twilio.com,
    not the Accounts REST base, so this cannot reuse _twilio_post."""
    data = urllib.parse.urlencode(form).encode("utf-8") if form else None
    token = base64.b64encode(
        ("%s:%s" % (creds["account_sid"], creds["auth_token"])).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Basic " + token,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("Twilio %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:400]))


GEO_BASE = "https://voice.twilio.com/v1/DialingPermissions/Countries"


def geo_permissions(codes, enable=None, creds=None):
    """Read, and optionally set, which countries this account may dial.

    Nothing outside your own country is dialable by default — an unenabled destination fails
    with 21215 before it rings, which is a safe failure but an opaque one.

    Only the LOW-RISK ranges are ever touched. The high-risk and toll-fraud ranges stay off
    permanently and are not exposed as a parameter: enabling those is the standard way an
    account gets drained by premium-rate fraud, and no MEET_RUDI patient will ever be on one.
    """
    creds = creds or _secret(TWILIO_SECRET) or {}
    if not creds.get("account_sid"):
        return {"ok": False, "error": "twilio credentials not configured"}

    results = {}
    if enable is not None:
        update = [{"iso_code": c,
                   "low_risk_numbers_enabled": bool(enable),
                   "high_risk_special_numbers_enabled": False,
                   "high_risk_tollfraud_numbers_enabled": False} for c in codes]
        written = _twilio_request(
            "https://voice.twilio.com/v1/DialingPermissions/BulkCountryUpdates", creds,
            {"UpdateRequest": json.dumps(update)}, method="POST")
        results["update_count"] = written.get("update_count")
        print("AUDIT: dialing permissions set low_risk=%s for %s" % (bool(enable), codes))

    # Always read back rather than trusting the write — the whole point is to know the state.
    results["countries"] = {}
    for code in codes:
        try:
            country = _twilio_request("%s/%s" % (GEO_BASE, code), creds)
            results["countries"][code] = {
                "name": country.get("name"),
                "low_risk_numbers_enabled": country.get("low_risk_numbers_enabled"),
                "high_risk_special_numbers_enabled": country.get(
                    "high_risk_special_numbers_enabled"),
                "high_risk_tollfraud_numbers_enabled": country.get(
                    "high_risk_tollfraud_numbers_enabled"),
            }
        except Exception as e:  # noqa: BLE001
            results["countries"][code] = {"error": str(e)}
    results["ok"] = True
    return results


def caller_ids(numbers=None, call_delay=0, creds=None):
    """Verify a number so a TRIAL account may dial it.

    Twilio places a verification call to the number and the person who answers must key in the
    six-digit code returned here — so the code has to reach a human before the phone rings,
    which is what `call_delay` buys.

    Only needed while the account is on trial. Upgrading removes the restriction entirely, and
    also removes the spoken "you have a trial account" announcement that Twilio prepends to
    every call — which matters more than it sounds, because that announcement lands in front of
    Rudi's opening and makes an experience test meaningless.
    """
    creds = creds or _secret(TWILIO_SECRET) or {}
    if not creds.get("account_sid"):
        return {"ok": False, "error": "twilio credentials not configured"}

    out = {"ok": True, "requested": {}}
    for number in (numbers or []):
        try:
            created = _twilio_post("OutgoingCallerIds.json", {
                "PhoneNumber": number,
                "FriendlyName": ("MEET_RUDI test %s" % number)[:64],
                "CallDelay": str(max(0, min(60, int(call_delay)))),
            }, creds)
            out["requested"][number] = {
                "validation_code": created.get("validation_code"),
                "call_sid": created.get("call_sid"),
            }
            print("AUDIT: caller-id verification requested for %s" % number)
        except Exception as e:  # noqa: BLE001
            out["requested"][number] = {"error": str(e)}

    # Read back what is already verified, so the result is the state and not just the attempt.
    try:
        listed = _twilio_request(
            "https://api.%s.twilio.com/2010-04-01/Accounts/%s/OutgoingCallerIds.json"
            % (TWILIO_REGION, creds["account_sid"]), creds)
        out["verified"] = [{"phone_number": c.get("phone_number"),
                            "friendly_name": c.get("friendly_name")}
                           for c in (listed.get("outgoing_caller_ids") or [])]
    except Exception as e:  # noqa: BLE001
        out["verified"] = {"error": str(e)}
    return out


def _hints(config):
    """Words Deepgram is most likely to mangle — the same seed the bench gives Whisper."""
    return ", ".join([b for b in (config.get("user_name"), config.get("topic")) if b])[:1000]


def place_call(config, dry_run=False, now=None):
    """Create the call record, then ask Twilio to dial. Returns (payload, status).

    `now` exists so the quiet-hours gate can be pinned in tests. Without it the suite passes or
    fails depending on what time of day it is run, which is not a test at all.
    """
    reason = gate(config, now)
    if reason:
        return {"ok": False, "skipped": reason}, 200

    call_id = calllog.new_call_id()
    state = brain.new_state(config)
    manifest = calllog.start(call_id, config, state, {"placed_by": "dispatcher"})

    if config.get("override_quiet_hours") and is_quiet(now, config.get("timezone")):
        manifest.setdefault("compliance", {})["quiet_hours_overridden"] = True
        print("AUDIT: quiet-hours overridden for call %s to %s" % (call_id, config.get("to")))

    twiml = relay.build_twiml(WS_URL, call_id, config.get("voice_attrs"), _hints(config),
                              language=config.get("language", "en"))
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
            expected = _dispatch_token()
            if not expected:
                return _response({"ok": False, "error": "dispatch auth not configured"}, 503)
            if (params.get("token") or "") != expected:
                return _response({"ok": False, "error": "unauthorized"}, 401)

        # One-off account administration: which countries this account may dial at all.
        if params.get("action") == "verify_caller_id":
            return _response(caller_ids(params.get("numbers"), params.get("call_delay", 0)))

        if params.get("action") == "geo":
            codes = [str(c).upper()[:2] for c in (params.get("countries") or [])][:25]
            if not codes:
                return _response({"ok": False, "error": "countries is required"}, 400)
            return _response(geo_permissions(codes, params.get("enable")))

        config = params.get("config") or {}
        if not isinstance(config, dict):
            return _response({"ok": False, "error": "config must be an object"}, 400)

        payload, status = place_call(config, dry_run=bool(params.get("dry_run")))
        return _response(payload, status)

    except Exception as e:  # noqa: BLE001
        print("ERROR: dispatch failed: %s" % e)
        return _response({"ok": False, "error": str(e)}, 500)
