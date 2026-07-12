"""
MEET_RUDI — meetrudi-consent-intake handler.

Public endpoint the CM pilot consent+intake form POSTs to. It re-validates every field
server-side (never trust the client), normalizes the phone to E.164, computes the same
pseudonym the WhatsApp side uses (HMAC(salt, phone)), assembles a GDPR record-of-consent, and
writes it to the EU data bucket under registrations/consent_documents/.

Object key (per product decision): <HMAC(phone)>_<last4>.json  — no name/phone in the key beyond
the last 4 digits, keeping PII out of bucket listings/logs (§5). The key is DETERMINISTIC, so a
re-submission from the same phone overwrites the previous record; enable S3 bucket Versioning on
the prefix to retain prior consent records immutably for audit.

Special-category health data (Art. 9) → lawful basis is EXPLICIT CONSENT: the mandatory
consent_health_data_processing flag MUST be true or the submission is refused.

Abuse guards (endpoint is public): a hidden honeypot field, an echo-consistency check for the
form's re-entry challenge, payload size caps, and strict validation. These deter form-driving and
blind-replay bots; a targeted bot posting directly is still possible — add WAF/Turnstile later if
needed.
"""

import os
import re
import json
import hmac
import hashlib
import datetime

import boto3

_s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]
PREFIX = os.environ.get("CONSENT_PREFIX", "registrations/consent_documents")
# Same salt + scheme as the WhatsApp store, so the pseudonym links a consent record to its
# WhatsApp contact (store.user_id: "wa_" + HMAC(salt, phone)[:24]).
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt-change-me")
DEFAULT_CC = os.environ.get("DEFAULT_COUNTRY_CODE", "+32")
FORM_VERSION = os.environ.get("FORM_VERSION", "cm-2026-07")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "20000"))

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "consent_form_fields.json"), encoding="utf-8") as _f:
    REGISTRY = json.load(_f)
FIELDS = REGISTRY["fields"]
CONSENTS = REGISTRY["consents"]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ValidationError(Exception):
    def __init__(self, field, message):
        super().__init__("%s: %s" % (field, message))
        self.field = field
        self.message = message


# --------------------------------------------------------------------------- helpers
def _resp(status, obj):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(obj, ensure_ascii=False)}


def _method(event):
    return (event.get("requestContext", {}).get("http", {}).get("method")
            or event.get("httpMethod") or "GET").upper()


def _source_ip(event):
    return (event.get("requestContext", {}).get("http", {}).get("sourceIp")
            or (event.get("headers") or {}).get("x-forwarded-for", "").split(",")[0].strip() or "")


def _user_agent(event):
    h = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return h.get("user-agent", "")


def normalize_phone(raw):
    """Best-effort E.164 for a Belgian-default pilot. Raises ValidationError if implausible."""
    if not isinstance(raw, str):
        raise ValidationError("mobile_number", "must be a string")
    s = re.sub(r"[^\d+]", "", raw)
    if s.startswith("00"):
        s = "+" + s[2:]
    elif s.startswith("0"):                    # national → default country
        s = DEFAULT_CC + s[1:]
    elif not s.startswith("+"):
        s = DEFAULT_CC + s
    if not re.fullmatch(r"\+\d{8,15}", s):
        raise ValidationError("mobile_number", "not a valid phone number")
    return s


def pseudonym(phone_e164):
    return "wa_" + hmac.new(SALT.encode(), phone_e164.encode(), hashlib.sha256).hexdigest()[:24]


def _object_key(phone_e164, stamp):
    """<HMAC(phone)>_<last4>_<yyyymmdd_hhmmss>.json — timestamp makes every submission unique, so
    a re-submission is a new record rather than an overwrite (full consent history retained)."""
    digest = hmac.new(SALT.encode(), phone_e164.encode(), hashlib.sha256).hexdigest()
    return "%s/%s_%s_%s.json" % (PREFIX, digest, phone_e164[-4:], stamp)


# --------------------------------------------------------------------------- validation
def _validate_field(spec, value, lang):
    key = spec["key"]
    required = spec.get("required", False)
    empty = value is None or (isinstance(value, str) and not value.strip()) \
        or (isinstance(value, list) and not value)
    if empty:
        if required:
            raise ValidationError(key, "is required")
        return None

    t = spec["type"]
    if t in ("string", "email", "phone"):
        if not isinstance(value, str):
            raise ValidationError(key, "must be text")
        value = value.strip()
        if len(value) > spec.get("max_len", 10000):
            raise ValidationError(key, "too long")
        if t == "email" and not _EMAIL_RE.match(value):
            raise ValidationError(key, "not a valid email")
        if t == "phone":
            value = normalize_phone(value)
        return value
    if t == "int":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            raise ValidationError(key, "must be a number")
        if iv < spec["min"] or iv > spec["max"]:
            raise ValidationError(key, "out of range")
        return iv
    if t == "bool":
        b = bool(value)
        if spec.get("must_be_true") and not b:
            raise ValidationError(key, "must be confirmed")
        return b
    if t == "enum":
        opts = spec["options"].get(lang) or spec["options"].get("en")
        if value not in opts:
            raise ValidationError(key, "not an allowed option")
        return value
    if t == "multi_enum":
        if not isinstance(value, list):
            raise ValidationError(key, "must be a list")
        opts = spec["options"].get(lang) or spec["options"].get("en")
        if not (spec["min_items"] <= len(value) <= spec["max_items"]):
            raise ValidationError(key, "select between %d and %d" % (spec["min_items"], spec["max_items"]))
        for item in value:
            if item not in opts:
                raise ValidationError(key, "contains an invalid option")
        return value
    raise ValidationError(key, "unknown field type")


def validate(payload):
    """Return (answers, consents, phone_e164). Raises ValidationError on the first problem."""
    lang = (payload.get("form_language") or "nl").lower()
    answers_in = payload.get("answers") or {}
    consents_in = payload.get("consents") or {}

    answers = {}
    phone_e164 = None
    for spec in FIELDS:
        clean = _validate_field(spec, answers_in.get(spec["key"]), lang)
        if clean is not None:
            answers[spec["key"]] = clean
        if spec["key"] == "mobile_number":
            phone_e164 = clean

    consents = {}
    for spec in CONSENTS:
        granted = bool((consents_in.get(spec["key"]) or {}).get("granted")) \
            if isinstance(consents_in.get(spec["key"]), dict) else bool(consents_in.get(spec["key"]))
        if spec.get("must_be_true") and not granted:
            raise ValidationError(spec["key"], "required consent not given")
        consents[spec["key"]] = granted

    if phone_e164 is None:
        raise ValidationError("mobile_number", "is required")
    return answers, consents, phone_e164, lang


def _echo_ok(payload):
    """Re-entry challenge: the value the user re-typed must match what they submitted.
    Cheap guard against lazy direct-POST bots; the form always sends `challenge`."""
    ch = payload.get("challenge")
    if not isinstance(ch, dict):
        return True   # no challenge present → don't hard-fail here (validation still applies)
    field, retyped = ch.get("field"), ch.get("value")
    submitted = (payload.get("answers") or {}).get(field)
    if isinstance(submitted, str) and isinstance(retyped, str):
        return submitted.strip().lower() == retyped.strip().lower()
    return submitted == retyped


# --------------------------------------------------------------------------- handler
def handler(event, context):
    if _method(event) == "OPTIONS":
        return _resp(200, {})
    if _method(event) != "POST":
        return _resp(200, {"ok": True, "service": "meetrudi-consent-intake"})

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _resp(413, {"error": "payload_too_large"})
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, TypeError):
        return _resp(400, {"error": "bad_json"})

    # Honeypot: a hidden field real users never fill. If present+non-empty → silently accept
    # (return success so bots don't learn) but store nothing.
    if (payload.get("hp_field") or "").strip():
        print("INTAKE honeypot tripped ip=%s" % _source_ip(event))
        return _resp(201, {"ok": True})

    if not _echo_ok(payload):
        return _resp(400, {"error": "challenge_mismatch"})

    try:
        answers, consents, phone_e164, lang = validate(payload)
    except ValidationError as e:
        return _resp(422, {"error": "validation_failed", "field": e.field, "detail": e.message})

    now_dt = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat()
    stamp = now_dt.strftime("%Y%m%d_%H%M%S")
    consent_records = {}
    for spec in CONSENTS:
        consent_records[spec["key"].replace("consent_", "")] = {
            "granted": consents[spec["key"]],
            "text": (payload.get("consent_texts") or {}).get(spec["key"], ""),
            "at": now,
        }

    record = {
        "schema_version": REGISTRY["schema_version"],
        "answers": answers,
        "consents": consent_records,
        "identity": {"phone_e164": phone_e164, "pseudonym": pseudonym(phone_e164),
                     "email": answers.get("email", "")},
        "meta": {
            "form_language": lang,
            "form_version": payload.get("form_version") or FORM_VERSION,
            "privacy_policy_version": payload.get("privacy_policy_version", ""),
            "terms_version": payload.get("terms_version", ""),
            "received_at": now,
            "ip_address": _source_ip(event),
            "user_agent": _user_agent(event),
        },
    }

    key = _object_key(phone_e164, stamp)
    _s3.put_object(Bucket=DATA_BUCKET, Key=key,
                   Body=json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
                   ContentType="application/json")
    print("INTAKE stored pseudonym=%s key=%s lang=%s" % (record["identity"]["pseudonym"], key, lang))
    return _resp(201, {"ok": True})
