"""
MEET_RUDI — operator alerts.

Raised when something needs a human, not a retry. Today that is one thing: an inbound objection
froze a conversation (see objection.py), and somebody has to find out how that number reached us.

Two channels on purpose, because they fail differently:

  S3 record   always written, needs no configuration, and is what the operator console lists and
              works from. This is the system of record for "was it investigated".
  Email       best-effort, to the support address. This is what actually reaches a person out of
              hours. A missing sender identity degrades to a log line rather than an exception —
              an unconfigured mailbox must never be the reason a freeze fails to take effect.

PII (§5): the S3 record stays in the AWS-EU plane and may carry the raw phone, exactly as
meta.json already does. The EMAIL leaves that plane, so it carries a masked number and the
pseudonymous ids only — enough to find the record, not enough to be the leak.
"""

import os
import json
import datetime

ALERT_PREFIX = os.environ.get("ALERT_PREFIX", "operator-alerts")
MAIL_FROM = os.environ.get("ALERT_MAIL_FROM", "") or os.environ.get("TESTER_MAIL_FROM", "")
MAIL_TO = os.environ.get("ALERT_MAIL_TO", "") or os.environ.get("TESTER_SUPPORT_EMAIL", "")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def mask_phone(phone):
    """+32487572671 -> +32*****2671. Enough to recognise, not enough to dial."""
    p = str(phone or "")
    if len(p) < 8:
        return "***"
    return p[:3] + "*" * (len(p) - 7) + p[-4:]


def _key(uid, at):
    return "%s/%s/%s-%s.json" % (ALERT_PREFIX, at.date().isoformat(), uid,
                                 at.strftime("%H%M%S"))


def _linked_tester(s3, bucket, phone):
    """Best-effort join back to the registration that supplied this number — the thing the
    operator has actually been asked to investigate. Never fatal: an alert with no tester is
    still an alert, and a wrong number very often has no tester at all."""
    try:
        import tester_store
        store = tester_store.TesterStore(s3, bucket)
        tester = store.find_by_phone(phone)
        if tester is None:
            return {}
        return {"tester_id": tester.tester_id, "email": tester.email,
                "status": tester.status, "created_at": tester.created_at,
                "locale": tester.locale}
    except Exception as e:  # noqa: BLE001
        print("WARN alert tester lookup failed: %s" % type(e).__name__)
        return {}


def _email(ses, subject, body):
    if not (ses and MAIL_FROM and MAIL_TO):
        print("INFO alert email not configured (from=%s to=%s)"
              % (bool(MAIL_FROM), bool(MAIL_TO)))
        return False
    try:
        ses.send_email(
            Source=MAIL_FROM,
            Destination={"ToAddresses": [MAIL_TO]},
            Message={"Subject": {"Data": subject, "Charset": "UTF-8"},
                     "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}},
        )
        return True
    except Exception as e:  # noqa: BLE001 - a bounce must not undo the freeze
        print("ERROR alert email failed: %s" % type(e).__name__)
        return False


def conversation_frozen(s3, bucket, uid, meta, detection, ses=None):
    """Record and announce that a thread was frozen by the objection gate.

    Returns the S3 key of the alert record so the caller can log the join key.
    """
    at = _now()
    phone = getattr(meta, "phone", "") or ""
    tester = _linked_tester(s3, bucket, phone)
    record = {
        "kind": "conversation_frozen",
        "at": at.isoformat(),
        "user_id": uid,
        "phone": phone,                      # EU plane only — see module docstring
        "locale": getattr(meta, "locale", ""),
        "detection": detection.to_dict() if hasattr(detection, "to_dict") else dict(detection),
        "tester": tester,
        "counts": {"inbound": getattr(meta, "msg_user", 0),
                   "total": getattr(meta, "msg_total", 0),
                   "proactive_sends": getattr(meta, "proactive_sends", 0)},
        "first_contact_at": getattr(meta, "created_at", ""),
        "status": "open",                    # operator flips this when investigated
    }
    key = _key(uid, at)
    s3.put_object(Bucket=bucket, Key=key,
                  Body=json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json")
    print("ALERT conversation_frozen uid=%s kind=%s key=%s"
          % (uid, record["detection"].get("kind"), key))

    d = record["detection"]
    # How many times we messaged them BEFORE they objected is the number that says how bad this
    # is, so it leads.
    body = (
        "A WhatsApp conversation was frozen by the objection gate.\n\n"
        "Reason        : %s (%s)\n"
        "Evidence      : %s\n"
        "Contact       : %s  (%s)\n"
        "We sent them  : %s proactive message(s), %s total\n"
        "First contact : %s\n"
        "Registration  : %s\n\n"
        "Nothing further will be sent to this number: the contact is frozen and all scheduled\n"
        "calls and proactive sends are cancelled. Please check how this number reached us, and\n"
        "if it is a wrong number, run an erasure (CLAUDE.md §5) rather than only unfreezing.\n\n"
        "Alert record  : s3://%s/%s\n"
    ) % (d.get("kind"), d.get("source"), d.get("evidence"),
         mask_phone(phone), uid,
         record["counts"]["proactive_sends"], record["counts"]["total"],
         record["first_contact_at"] or "unknown",
         tester.get("tester_id") or "no linked registration found",
         bucket, key)
    _email(ses, "[Rudi] Conversation frozen — %s" % d.get("kind"), body)
    return key
