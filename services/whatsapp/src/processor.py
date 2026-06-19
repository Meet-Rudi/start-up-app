"""
MEET_RUDI — meetrudi-wa-processor handler.

Consumes inbound WhatsApp messages from the FIFO queue, pseudonymizes the phone number,
generates a reply, and sends it back via the provider. Logs each exchange.

MILESTONE SCOPE: stateless single-turn responder (no memory / window-state yet). Per-user
state + memory + the full stateful Rudi engine are the NEXT infra step. Media is acknowledged
(fetch/vision/ASR come later).
"""

import os
import json
import hmac
import hashlib
import datetime

import boto3

import provider
import gateway

s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]
LOG_KEY = os.environ.get("WA_LOG_KEY", "external_questions/whatsapp_messages.jsonl")
RUDI_CONTEXT_KEY = "contexts/rudi-context.md"
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt")

SYSTEM = (
    "You are Rudi, a warm, encouraging health & lifestyle accountability buddy chatting on "
    "WhatsApp. Reply briefly and kindly (1-3 short sentences). Never give medical advice; for "
    "anything clinical, gently suggest the person speak with their doctor or care team. If "
    "someone signals self-harm or crisis, share https://findahelpline.com/countries/be . "
    "Reply in the user's language."
)

_ctx = {}


def _context():
    if "c" in _ctx:
        return _ctx["c"]
    try:
        _ctx["c"] = s3.get_object(Bucket=DATA_BUCKET, Key=RUDI_CONTEXT_KEY)["Body"].read().decode("utf-8")
    except Exception:  # noqa: BLE001
        _ctx["c"] = ""
    return _ctx["c"]


def _user_id(phone):
    # Pseudonymous, stable id. Raw phone (PII) stays in the AWS-EU plane; logs/AI use this id.
    return "wa_" + hmac.new(SALT.encode(), phone.encode(), hashlib.sha256).hexdigest()[:24]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _log(rec):
    line = json.dumps(rec, ensure_ascii=False)
    try:
        try:
            existing = s3.get_object(Bucket=DATA_BUCKET, Key=LOG_KEY)["Body"].read()
        except s3.exceptions.NoSuchKey:
            existing = b""
        s3.put_object(Bucket=DATA_BUCKET, Key=LOG_KEY,
                      Body=existing + line.encode("utf-8") + b"\n",
                      ContentType="application/x-ndjson")
    except Exception as e:  # noqa: BLE001
        print("WARN log: %s" % e)


def _respond(msg):
    text = msg.get("text", "")
    if msg.get("type") in ("image", "audio", "media") and not text:
        return "Thanks, I got your %s! 👍 I'll be able to look at these properly soon." % msg.get("type")
    sys = SYSTEM + ("\n\n# About me\n" + _context() if _context() else "")
    messages = [{"role": "system", "content": sys}, {"role": "user", "content": text}]
    return gateway.generate(messages, json_mode=False)["text"]


def handler(event, context):
    for record in event.get("Records", []):
        phone = ""
        try:
            msg = json.loads(record["body"])
            phone = msg.get("user_phone", "")
            reply = _respond(msg)
            provider.send_text(phone, reply)
            _log({"at": _now(), "user": _user_id(phone), "type": msg.get("type"),
                  "in": msg.get("text", ""), "out": reply, "msg_id": msg.get("provider_msg_id")})
        except gateway.AllRateLimited:
            if phone:
                try:
                    provider.send_text(phone, "😴 I'm resting for a bit — message me again in a little while!")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            print("ERROR processor: %s" % e)
            if phone:
                try:
                    provider.send_text(phone, "Sorry — I had a hiccup. Please try again in a moment. 💬")
                except Exception:  # noqa: BLE001
                    pass
    return {"ok": True}
