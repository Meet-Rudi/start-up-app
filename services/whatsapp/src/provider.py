"""
MEET_RUDI — WhatsApp provider (Twilio implementation).

The single swap-point for the WhatsApp channel. Webhook + processor only touch this module,
so moving Twilio -> 360dialog / Meta Cloud API later is localized here.

Pure stdlib + boto3 (runtime-provided): signature validation, inbound parse/normalize, media
fetch, and outbound send via the Twilio REST API. Zero pip dependencies.
"""

import os
import json
import hmac
import base64
import hashlib
import urllib.parse
import urllib.request
import urllib.error

import boto3

_secrets = boto3.client("secretsmanager")
_cache = {}

TWILIO_SECRET = os.environ.get("TWILIO_SECRET", "meetrudi/whatsapp/twilio")
FROM_NUMBER = os.environ.get("WHATSAPP_FROM", "")          # e.g. "whatsapp:+14155238886"
VALIDATE_SIGNATURE = os.environ.get("VALIDATE_SIGNATURE", "true").lower() == "true"
WEBHOOK_URL_OVERRIDE = os.environ.get("WEBHOOK_URL", "")   # set if URL reconstruction mismatches


def _creds():
    if "creds" in _cache:
        return _cache["creds"]
    raw = _secrets.get_secret_value(SecretId=TWILIO_SECRET).get("SecretString", "") or "{}"
    obj = json.loads(raw)
    _cache["creds"] = {
        "sid": obj.get("account_sid") or obj.get("sid"),
        "token": obj.get("auth_token") or obj.get("token"),
    }
    return _cache["creds"]


class NormalizedMessage:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self):
        return dict(self.__dict__)


# --------------------------------------------------------------------------- inbound
def parse_body(event):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _reconstruct_url(event):
    if WEBHOOK_URL_OVERRIDE:
        base = WEBHOOK_URL_OVERRIDE
    else:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        base = "https://" + headers.get("host", "") + event.get("rawPath", "/")
    qs = event.get("rawQueryString", "")
    return base + (("?" + qs) if qs else "")


def verify_signature(event, params):
    """Validate Twilio's X-Twilio-Signature (HMAC-SHA1 of URL + sorted key+value pairs)."""
    if not VALIDATE_SIGNATURE:
        return True
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    sig = headers.get("x-twilio-signature", "")
    if not sig:
        return False
    s = _reconstruct_url(event)
    for k in sorted(params.keys()):
        s += k + params[k]
    digest = hmac.new(_creds()["token"].encode(), s.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, sig)


def normalize(params):
    frm = params.get("From", "")          # "whatsapp:+E164"
    to = params.get("To", "")
    num_media = int(params.get("NumMedia", "0") or 0)
    media = []
    for i in range(num_media):
        media.append({
            "url": params.get("MediaUrl%d" % i),
            "content_type": params.get("MediaContentType%d" % i),
        })
    msg_type = "text"
    if num_media > 0:
        ct = media[0].get("content_type") or ""
        msg_type = "image" if ct.startswith("image") else "audio" if ct.startswith("audio") else "media"
    return NormalizedMessage(
        provider_msg_id=params.get("MessageSid", ""),
        user_phone=frm.replace("whatsapp:", ""),
        our_number=to.replace("whatsapp:", ""),
        type=msg_type,
        text=params.get("Body", "") or "",
        media=media,
    )


def fetch_media(url):
    c = _creds()
    auth = base64.b64encode(("%s:%s" % (c["sid"], c["token"])).encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(), r.headers.get("Content-Type")


# --------------------------------------------------------------------------- outbound
def _post_twilio(params):
    c = _creds()
    url = "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % c["sid"]
    data = urllib.parse.urlencode(params).encode()
    auth = base64.b64encode(("%s:%s" % (c["sid"], c["token"])).encode()).decode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": "Basic " + auth,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("Twilio HTTP %s: %s" % (e.code, detail))


def _to(to_phone):
    return "whatsapp:+" + to_phone.lstrip("+")


def send_text(to_phone, body):
    return _post_twilio({"From": FROM_NUMBER, "To": _to(to_phone), "Body": body})


def send_media(to_phone, body, media_url):
    p = {"From": FROM_NUMBER, "To": _to(to_phone), "MediaUrl": media_url}
    if body:
        p["Body"] = body
    return _post_twilio(p)


def send_template(to_phone, content_sid, variables=None):
    """Out-of-window templated message (approved Content template)."""
    p = {"From": FROM_NUMBER, "To": _to(to_phone), "ContentSid": content_sid}
    if variables:
        p["ContentVariables"] = json.dumps(variables)
    return _post_twilio(p)
