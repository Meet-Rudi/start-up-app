"""
E-mail notifications for adoption requests, sent through SES in eu-central-1.

Two mails, both to NOTIFY_TO with NOTIFY_CC in copy:

    started    a request's first step (contact details, with the contact agreement) is saved
               subject: New prospect started configuration - <name> | <e-mail>
    complete   the request is sent: every answer, step by step, in one table
               subject: New Rudi clone REQUEST COMPLETE - <name> | <e-mail>

PII (CLAUDE.md §0.1, §5): unlike the operator alerts (services/whatsapp/src/alerts.py), these
mails carry the prospect's name, contact details and answers by design, to our own team, about a
business contact who asked us to get in touch (decision #48). They never carry an end-user's
data. Logs carry the request id and SES's message id, never an address or an answer.

Best-effort: the S3 record is the system of record, so a mail that cannot be sent is logged and
never fails the save. Unconfigured (no sender or no recipient) means no mail, not an error.
Addresses come from the stack's parameters (deploy.local.json), never from source.
"""

from __future__ import annotations

import datetime
import html
import json
import os
from email.utils import formataddr

import boto3
import questionnaire as qn
from botocore.config import Config


def _addresses(name: str) -> list[str]:
    return [a.strip() for a in os.environ.get(name, "").split(",") if a.strip()]


MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "").strip() or "Rudi request form"
NOTIFY_TO = _addresses("NOTIFY_TO")
NOTIFY_CC = _addresses("NOTIFY_CC")
BUCKET = os.environ.get("DATA_BUCKET", "")
SUBJECT_MAX = 200

# Short timeouts: the person is waiting on the save this mail follows.
_ses = boto3.client(
    "ses",
    config=Config(
        connect_timeout=2,
        read_timeout=4,
        retries={"max_attempts": 2, "mode": "standard"},
    ),
)

# Rudi's colours (docs/design/rudi-brand.md), inline because mail clients drop style sheets.
PETROL, MUTED, IVORY, PAPER, BUTTER, LINE = (
    "#244a4c",
    "#48615f",
    "#f8f5ec",
    "#fffcf5",
    "#e6cf79",
    "#e6e0d0",
)
FONT = "'DM Sans', Arial, Helvetica, sans-serif"


# ---------------------------------------------------------------------------------- helpers
def _log(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}))


def _when(iso: str | None) -> str:
    if not iso:
        return qn.NO_ANSWER
    try:
        at = datetime.datetime.fromisoformat(iso).astimezone(datetime.UTC)
    except ValueError:
        return iso
    return at.strftime("%Y-%m-%d %H:%M UTC")


def _subject(prefix: str, contact: dict) -> str:
    name = contact.get("contact_name") or qn.NO_ANSWER
    email = contact.get("contact_email") or qn.NO_ANSWER
    subject = f"{prefix} - {name} | {email}"
    # Answers are stored with whitespace collapsed; this keeps a header a single line regardless.
    return " ".join(subject.split())[:SUBJECT_MAX]


def _about(record: dict, key: str) -> dict:
    rows = [
        {"label": "Request id", "value": record["id"]},
        {"label": "Started", "value": _when(record.get("started_at"))},
    ]
    if record.get("completed_at"):
        rows.append({"label": "Sent", "value": _when(record["completed_at"])})
    rows += [
        {
            "label": "Form version",
            "value": f"{record['questionnaire']} · {record['form_version']}",
        },
        {"label": "Stored at", "value": f"s3://{BUCKET}/{key}"},
    ]
    return {"number": None, "title": "About this request", "rows": rows}


# ----------------------------------------------------------------------------------- bodies
def _html(eyebrow: str, heading: str, lead: str, sections: list[dict]) -> str:
    e = html.escape
    # Long values (a storage path, a pasted URL) wrap inside the card instead of widening it.
    cell = (
        f"padding:9px 12px;border-bottom:1px solid {LINE};vertical-align:top;"
        "overflow-wrap:anywhere;word-break:break-word;"
    )
    rows = []
    for s in sections:
        title = f"{s['number']:02d} · {s['title']}" if s.get("number") else s["title"]
        rows.append(
            f'<tr><td colspan="2" style="padding:9px 12px;background:{BUTTER};color:{PETROL};'
            f'font-weight:700;font-size:14px">{e(title)}</td></tr>'
        )
        for r in s["rows"]:
            value = e(r["value"]).replace("\n", "<br>")
            rows.append(
                f'<tr><td style="{cell}width:40%;color:{MUTED}">{e(r["label"])}</td>'
                f'<td style="{cell}color:{PETROL}">{value}</td></tr>'
            )
    return (
        f'<div style="background:{IVORY};padding:24px 12px;font-family:{FONT};color:{PETROL};'
        f'line-height:1.5">'
        f'<div style="max-width:680px;margin:0 auto;background:{PAPER};border:1px solid {LINE};'
        f'border-radius:16px;padding:28px">'
        f'<p style="margin:0 0 8px;font-size:11px;font-weight:700;letter-spacing:.15em;'
        f'text-transform:uppercase">{e(eyebrow)}</p>'
        f'<h1 style="margin:0 0 10px;font-size:24px;font-weight:500;line-height:1.25">'
        f"{e(heading)}</h1>"
        f'<p style="margin:0 0 22px;font-size:15px;color:{MUTED}">{e(lead)}</p>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;table-layout:fixed;font-size:14px">{"".join(rows)}</table>'
        f"</div></div>"
    )


def _text(heading: str, lead: str, sections: list[dict]) -> str:
    lines = [heading, "", lead, ""]
    for s in sections:
        title = f"{s['number']:02d} · {s['title']}" if s.get("number") else s["title"]
        lines += [title.upper(), "-" * len(title)]
        for r in s["rows"]:
            lines.append(f"{r['label']}: {r['value']}")
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------------- mails
def started_mail(q: dict, record: dict, key: str) -> tuple[str, str, str]:
    contact = record["answers"].get("contact") or {}
    org = contact.get("organisation") or "An organisation"
    heading = f"{org} started a Rudi request."
    lead = (
        "They saved the first step and agreed to be contacted. Their answers are saved as they "
        "go; if they stop, the request waits under platform/prospects/partial/ for a follow-up."
    )
    sections = [
        *qn.answer_table(q, record["answers"], steps=("contact",)),
        _about(record, key),
    ]
    return (
        _subject("New prospect started configuration", contact),
        _html("New prospect", heading, lead, sections),
        _text(heading, lead, sections),
    )


def complete_mail(q: dict, record: dict, key: str) -> tuple[str, str, str]:
    contact = record["answers"].get("contact") or {}
    org = contact.get("organisation") or "An organisation"
    table = qn.answer_table(q, record["answers"])
    heading = f"{org} sent a Rudi request."
    lead = (
        f"All {len(table)} steps are saved. Their answers, step by step; "
        f"{qn.NO_ANSWER} marks a question they left open."
    )
    sections = [*table, _about(record, key)]
    return (
        _subject("New Rudi clone REQUEST COMPLETE", contact),
        _html("Request complete", heading, lead, sections),
        _text(heading, lead, sections),
    )


def send(kind: str, prospect_id: str, mail: tuple[str, str, str]) -> bool:
    """Send one notification. Never raises."""
    if not (MAIL_FROM and NOTIFY_TO):
        _log(
            "prospect.notify_skipped",
            prospect=prospect_id,
            kind=kind,
            reason="not_configured",
        )
        return False
    subject, html_body, text_body = mail
    destination: dict = {"ToAddresses": NOTIFY_TO}
    if NOTIFY_CC:
        destination["CcAddresses"] = NOTIFY_CC
    try:
        resp = _ses.send_email(
            Source=formataddr((MAIL_FROM_NAME, MAIL_FROM)),
            Destination=destination,
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
    except Exception as e:  # noqa: BLE001 — a throttle or a refusal must not fail the save
        code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "")
        _log(
            "prospect.notify_failed",
            prospect=prospect_id,
            kind=kind,
            code=code or type(e).__name__,
        )
        return False
    _log(
        "prospect.notified",
        prospect=prospect_id,
        kind=kind,
        message=resp.get("MessageId", ""),
    )
    return True
