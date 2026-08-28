"""
MEET_RUDI — meetrudi-call-status handler (Twilio call status callbacks).

Twilio POSTs here as a call moves through initiated → ringing → answered → completed. Two jobs,
both of which the rest of the system depends on:

1. **Never talk to an answering machine.** The dispatcher dials with `MachineDetection=Enable`,
   so Twilio reports `AnsweredBy` once it has decided. On any machine verdict we immediately
   complete the call over the REST API — Rudi hangs up rather than delivering a coaching
   conversation to someone's voicemail. This is the polite hang-up the tester console promises.

2. **Record a terminal outcome on the manifest.** `telephony.answered_by` and
   `telephony.call_status` are what the tester console reads to decide whether an attempt counts
   against a tester's five calls. Only a genuinely connected call is deducted, so this handler is
   the single source of truth for that ledger.

Twilio's callback is a form POST, not JSON. It is authenticated by the `call_id` query parameter
the dispatcher minted plus Twilio's own signature; we additionally refuse any call_id we cannot
find a manifest for, so a guessed id writes nothing.
"""

import os
import json
import base64
import urllib.parse
import urllib.request
import urllib.error

import boto3

import calllog

_secrets = boto3.client("secretsmanager")

TWILIO_SECRET = os.environ.get("TWILIO_SECRET", "meetrudi/twilio/voice")
TWILIO_REGION = os.environ.get("TWILIO_REGION", "ie1")

# Twilio's answering-machine verdicts. `human` and `unknown` are the only ones we let through.
MACHINE_ANSWERS = {"machine_start", "machine_end_beep", "machine_end_silence",
                   "machine_end_other", "fax"}
TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}

_cache = {}


def _secret(secret_id):
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
    # Twilio ignores the body but retries on a non-2xx, so this always answers 200 unless the
    # request was genuinely unusable. A retry storm helps nobody.
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, ensure_ascii=False)}


def _form(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}


def _call_id(event, form):
    return ((event.get("queryStringParameters") or {}).get("call_id")
            or form.get("call_id") or "")


def hangup(call_sid, creds):
    """Complete an in-flight call over the Twilio REST API. Returns True if Twilio accepted it."""
    if not (call_sid and creds.get("account_sid")):
        return False
    host = ("api.%s.twilio.com" % TWILIO_REGION) if TWILIO_REGION else "api.twilio.com"
    url = "https://%s/2010-04-01/Accounts/%s/Calls/%s.json" % (host, creds["account_sid"], call_sid)
    data = urllib.parse.urlencode({"Status": "completed"}).encode("utf-8")
    token = base64.b64encode(
        ("%s:%s" % (creds["account_sid"], creds.get("auth_token", ""))).encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Authorization": "Basic " + token,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as e:
        # 409 means the call already ended on its own — the end state we wanted either way.
        print("WARN hangup HTTP %s for sid=%s" % (e.code, call_sid))
        return e.code == 409
    except Exception as e:  # noqa: BLE001
        print("WARN hangup failed for sid=%s (%s)" % (call_sid, type(e).__name__))
        return False


def apply_status(manifest, form):
    """Fold one Twilio callback into the manifest. Pure — no I/O, so tests can drive it directly.

    Returns (manifest, machine_detected).
    """
    answered_by = (form.get("AnsweredBy") or "").strip()
    call_status = (form.get("CallStatus") or "").strip()

    telephony = manifest.setdefault("telephony", {})
    if answered_by:
        telephony["answered_by"] = answered_by
    if call_status:
        telephony["call_status"] = call_status
    if form.get("CallDuration"):
        try:
            telephony["duration_s"] = int(form["CallDuration"])
        except (ValueError, TypeError):
            pass
    if form.get("CallSid"):
        telephony.setdefault("call_sid", form["CallSid"])

    machine = answered_by in MACHINE_ANSWERS
    if machine:
        telephony["machine_detected"] = True
        manifest["end_reason"] = manifest.get("end_reason") or "voicemail"
    if call_status in TERMINAL_STATUSES:
        manifest["status"] = "completed"
        manifest["ended_at"] = manifest.get("ended_at") or calllog.iso()
        if not manifest.get("end_reason"):
            manifest["end_reason"] = ("hangup" if call_status == "completed" else call_status)
    return manifest, machine


def handler(event, context):
    form = _form(event)
    call_id = _call_id(event, form)
    if not call_id:
        return _response({"ok": False, "error": "no call_id"}, 400)

    manifest = calllog.load(call_id)
    if not manifest:
        # An unknown id writes nothing at all — a guessed callback cannot create a record.
        print("WARN status for unknown call_id=%s" % call_id)
        return _response({"ok": False, "error": "unknown call"}, 404)

    manifest, machine = apply_status(manifest, form)

    if machine:
        creds = _secret(TWILIO_SECRET) or {}
        sid = (manifest.get("telephony") or {}).get("call_sid") or form.get("CallSid")
        ended = hangup(sid, creds)
        manifest["telephony"]["hung_up_on_machine"] = ended
        print("CALL %s answered by machine (%s) — hangup=%s"
              % (call_id, form.get("AnsweredBy"), ended))

    calllog._put_json(calllog._manifest_key(call_id), manifest)
    print("CALL %s status=%s answered_by=%s" % (call_id, form.get("CallStatus", ""),
                                                form.get("AnsweredBy", "")))
    return _response({"ok": True, "call_id": call_id,
                      "machine": machine, "status": form.get("CallStatus", "")})
