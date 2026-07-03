"""
MEET_RUDI — meetrudi-wa-sendtest: throwaway utility to verify Twilio WhatsApp OUTBOUND.

Token-gated Function URL you can hit from a browser:
    https://<url>/?token=<console-token>&to=+32470...&body=Hello

It exercises the real send path (`provider.send_text` → Twilio), isolated from the console /
store / inbound webhook — so a failure here is unambiguously a Twilio/creds problem. Remove
this function once outbound is confirmed; it is not part of the product path.
"""

import os
import hmac
import json

import boto3

import provider

_secrets = boto3.client("secretsmanager")
TOKEN_SECRET = os.environ.get("CONSOLE_TOKEN_SECRET", "")
_cache: dict = {}


def _token():
    if "t" in _cache:
        return _cache["t"]
    if not TOKEN_SECRET:
        _cache["t"] = ""
        return ""
    raw = _secrets.get_secret_value(SecretId=TOKEN_SECRET).get("SecretString", "") or "{}"
    try:
        _cache["t"] = json.loads(raw).get("token", "")
    except Exception:  # noqa: BLE001 - allow a bare-string secret
        _cache["t"] = raw.strip()
    return _cache["t"]


def _resp(code, obj):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(obj, ensure_ascii=False)}


def handler(event, context):
    q = event.get("queryStringParameters") or {}
    want = _token()
    if not want or not hmac.compare_digest(q.get("token", ""), want):
        return _resp(401, {"error": "unauthorized"})

    # In a URL query '+' decodes to a space, so accept digits with or without it and normalize.
    to = (q.get("to", "") or "").strip().replace(" ", "")
    body = q.get("body") or "Test message from Rudi 👋"
    if not to:
        return _resp(400, {"error": "missing 'to' (country code + number, e.g. 32470123456)"})
    try:
        res = provider.send_text(to, body)
        return _resp(200, {"ok": True, "sid": (res or {}).get("sid"),
                           "status": (res or {}).get("status")})
    except Exception as e:  # noqa: BLE001 - surface Twilio's error for debugging
        print("ERROR sendtest: %s" % e)
        return _resp(502, {"ok": False, "error": str(e)[:300]})
