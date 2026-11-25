"""
meetrudi-internal-prod-prospect-intake — the public endpoint behind Rudi's adoption request form.

An organisation that wants its own Rudi fills in the request form on the company website. The
form saves after every step, so a request that stops halfway still leaves us its contact details
to follow up on. One Function URL, no sign-in; every call is a JSON POST with an `action`:

    open     the first step (contact details) and the step titles        anyone
    start    store the contact step; mint the request id and its token   anyone
    save     store one step (`draft` skips the required checks)          id + token
    load     one step the person has reached, with its saved answers     id + token
    resume   where the person left off                                   id + token

The questionnaire lives here (questionnaire.json), not in the page: the browser gets a step only
once every step before it is saved, so the public page shows the contact step and nothing more.

Storage (EU data bucket, objects of no tenant under platform/, conventions §2):

    platform/prospects/partial/prs_….partial.json     started, not sent: the follow-up list
    platform/prospects/complete/prs_….complete.json   sent

The last step's full save moves the request from partial/ to complete/. Each record says its own
`state` too, and a follow-up job skips a partial whose complete twin exists (rules for that job:
web/adoption-request/README.md).

The token is returned once and kept by the browser; the record holds only its SHA-256. Logs carry
the request id and step, never contact details (CLAUDE.md §5). Abuse guards for a public endpoint:
body size cap, a honeypot, a minimum fill time on the first step, strict validation against the
questionnaire, and steps that cannot be read ahead of the saved ones.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets

import boto3
import questionnaire as qn
from rudi_platform.ids import Cat, is_id, new_id
from rudi_platform.naming import platform_s3_key

_s3 = boto3.client("s3")
BUCKET = os.environ["DATA_BUCKET"]
RELEASE = os.environ.get("RELEASE", "")
RECORD_SCHEMA = "rudi-prospect@1"
MAX_BODY_BYTES = 32_000
MIN_FILL_MS = 1_500
MAX_EVENTS = 200

Q = qn.load()
qn.check(Q)


# ---------------------------------------------------------------------------------- helpers
def partial_key(prospect_id: str) -> str:
    return platform_s3_key("prospects", "partial", f"{prospect_id}.partial.json")


def complete_key(prospect_id: str) -> str:
    return platform_s3_key("prospects", "complete", f"{prospect_id}.complete.json")


class Refused(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error", "refused"))
        self.status = status
        self.body = body


def _resp(status: int, obj: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(obj, ensure_ascii=False),
    }


def _log(event: str, **fields) -> None:
    print(json.dumps({"event": event, "release": RELEASE, **fields}))


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _error_code(exc: Exception) -> str:
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")


def _get(key: str) -> dict | None:
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
    except Exception as e:
        if _error_code(e) in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return json.loads(obj["Body"].read())


def _put(key: str, record: dict) -> None:
    _s3.put_object(
        Bucket=BUCKET,
        Key=key,
        ContentType="application/json",
        Body=json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"),
    )


# ----------------------------------------------------------------------------------- record
def _lang(payload: dict) -> str:
    lang = payload.get("lang")
    return lang if lang in Q["languages"] else Q["languages"][0]


def _refresh(record: dict, kind: str, step_key: str) -> None:
    """Recompute everything derived from the answers: progress, agreements, follow-up."""
    now = _now()
    answers = record["answers"]
    record["updated_at"] = now
    record["progress"] = qn.progress(Q, answers)

    agreements = record.setdefault("agreements", {})
    for step in Q["steps"]:
        if step["key"] not in answers:
            continue
        for f in qn.value_fields(step):
            if f["type"] != "agree":
                continue
            granted = answers[step["key"]].get(f["key"]) is True
            before = agreements.get(f["key"])
            if granted and not (before and before["granted"]):
                agreements[f["key"]] = {"granted": True, "text": f["label"], "at": now}
            elif not granted and before and before["granted"]:
                agreements[f["key"]] = {"granted": False, "text": f["label"], "at": now}

    contact = answers.get("contact") or {}
    next_step = record["progress"]["next_step"]
    next_def = qn.step_by_key(Q, next_step) if next_step else None
    record["follow_up"] = {
        **{k: contact.get(k) for k in qn.CONTACT_KEYS},
        "may_contact": (agreements.get("contact_agreement") or {}).get(
            "granted", False
        ),
        "wants_updates": (answers.get("send") or {}).get("updates", False),
        "stopped_before": next_step,
        "stopped_before_title": next_def["title"] if next_def else None,
        "last_activity_at": now,
    }
    events = record.setdefault("events", [])
    events.append({"at": now, "kind": kind, "step": step_key})
    del events[:-MAX_EVENTS]


def _new_record(prospect_id: str, token: str, lang: str, contact: dict) -> dict:
    now = _now()
    record = {
        "record_schema": RECORD_SCHEMA,
        "id": prospect_id,
        "state": "partial",
        "questionnaire": Q["questionnaire"],
        "form_version": Q["form_version"],
        "lang": lang,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "token_sha256": _digest(token),
        "answers": {"contact": contact},
    }
    _refresh(record, "start", "contact")
    return record


def _open_record(payload: dict) -> dict:
    """The stored request named by `id`, if `token` is its token. Raises Refused."""
    prospect_id, token = payload.get("id"), payload.get("token")
    if not is_id(prospect_id, Cat.PROSPECT) or not isinstance(token, str) or not token:
        raise Refused(400, {"error": "bad_request"})
    record = _get(partial_key(prospect_id)) or _get(complete_key(prospect_id))
    if record is None:
        raise Refused(404, {"error": "not_found"})
    if not hmac.compare_digest(record.get("token_sha256", ""), _digest(token)):
        _log("prospect.bad_token", prospect=prospect_id)
        raise Refused(403, {"error": "forbidden"})
    # The questionnaire may have changed since the last save; progress follows the current one.
    record["progress"] = qn.progress(Q, record["answers"])
    return record


def _reachable(record: dict, step_key: str) -> dict:
    """The step, if the person may see it: a saved step, or the first one still open."""
    answers = record["answers"]
    steps = qn.applicable_steps(Q, answers)
    keys = [s["key"] for s in steps]
    if step_key not in keys:
        raise Refused(404, {"error": "no_such_step"})
    first_open = record["progress"]["next_step"]
    limit = keys.index(first_open) if first_open in keys else len(keys) - 1
    if keys.index(step_key) > limit:
        raise Refused(403, {"error": "not_yet", "step": first_open})
    return steps[keys.index(step_key)]


def _view(record: dict, step: dict) -> dict:
    """A step for the browser, with what the person saved in it (None if never saved)."""
    answers = record["answers"]
    return {
        "section": qn.public_step(Q, step, answers),
        "answers": answers.get(step["key"]),
    }


def _status(record: dict) -> dict:
    return {
        "steps": qn.steps_summary(Q, record["answers"]),
        "progress": record["progress"],
    }


# ----------------------------------------------------------------------------------- actions
def act_open(payload: dict) -> tuple[int, dict]:
    first = Q["steps"][0]
    return 200, {
        "form": {
            "questionnaire": Q["questionnaire"],
            "form_version": Q["form_version"],
        },
        "steps": qn.steps_summary(Q, {}),
        "progress": qn.progress(Q, {}),
        "section": qn.public_step(Q, first, {}),
        "answers": None,
    }


def act_start(payload: dict) -> tuple[int, dict]:
    lang = _lang(payload)
    elapsed = payload.get("elapsed_ms")
    is_bot = bool(str(payload.get("hp") or "").strip()) or (
        isinstance(elapsed, (int, float)) and elapsed < MIN_FILL_MS
    )
    if is_bot:
        # Look like success, store nothing, show nothing further.
        _log("prospect.bot_dropped")
        return 201, {
            "id": new_id(Cat.PROSPECT),
            "token": secrets.token_urlsafe(32),
            "steps": [],
            "progress": qn.progress(Q, {}),
            "next": None,
        }

    contact = qn.clean_step(Q["steps"][0], payload.get("answers"), {}, draft=False)
    prospect_id, token = new_id(Cat.PROSPECT), secrets.token_urlsafe(32)
    record = _new_record(prospect_id, token, lang, contact)
    _put(partial_key(prospect_id), record)
    _log(
        "prospect.started", prospect=prospect_id, percent=record["progress"]["percent"]
    )

    steps = qn.applicable_steps(Q, record["answers"])
    return 201, {
        "id": prospect_id,
        "token": token,
        **_status(record),
        "next": _view(record, steps[1]) if len(steps) > 1 else None,
    }


def _complete(record: dict) -> None:
    prospect_id = record["id"]
    record["state"] = "complete"
    record["completed_at"] = record["updated_at"]
    _put(complete_key(prospect_id), record)
    try:
        _s3.delete_object(Bucket=BUCKET, Key=partial_key(prospect_id))
    except Exception as e:  # noqa: BLE001
        # No delete right yet (infra/iam/meetrudi-lambda-runner.prospects-add.json): leave the
        # partial saying it is complete, so nothing follows up on it.
        _put(partial_key(prospect_id), record)
        _log(
            "prospect.partial_kept",
            prospect=prospect_id,
            code=_error_code(e) or type(e).__name__,
        )


def act_save(payload: dict) -> tuple[int, dict]:
    record = _open_record(payload)
    if record["state"] == "complete":
        raise Refused(409, {"error": "already_sent"})
    step = _reachable(record, payload.get("step"))
    draft = payload.get("draft") is True
    answers = record["answers"]
    earlier = qn.flatten({k: v for k, v in answers.items() if k != step["key"]})
    answers[step["key"]] = qn.clean_step(step, payload.get("answers"), earlier, draft)
    if not draft and step["key"] == "contact":
        record["lang"] = _lang(payload)

    steps = qn.applicable_steps(Q, answers)
    keys = [s["key"] for s in steps]
    is_last = step["key"] == keys[-1]
    _refresh(record, "draft" if draft else "save", step["key"])
    prospect_id = record["id"]

    if not draft and is_last:
        open_step = record["progress"]["next_step"]
        if open_step is None:
            _refresh(record, "complete", step["key"])
            _complete(record)
            _log("prospect.completed", prospect=prospect_id)
            return 200, {**_status(record), "complete": True, "next": None}
        _put(partial_key(prospect_id), record)
        _log("prospect.incomplete", prospect=prospect_id, step=open_step)
        return 409, {
            "error": "incomplete",
            **_status(record),
            "next": _view(record, qn.step_by_key(Q, open_step)),
        }

    _put(partial_key(prospect_id), record)
    _log(
        "prospect.saved",
        prospect=prospect_id,
        step=step["key"],
        draft=draft,
        percent=record["progress"]["percent"],
    )
    following = None
    if not draft and step["key"] in keys and not is_last:
        following = _view(record, steps[keys.index(step["key"]) + 1])
    return 200, {**_status(record), "complete": False, "next": following}


def act_load(payload: dict) -> tuple[int, dict]:
    record = _open_record(payload)
    if record["state"] == "complete":
        return 200, {"complete": True}
    step = _reachable(record, payload.get("step"))
    return 200, {**_status(record), "complete": False, **_view(record, step)}


def act_resume(payload: dict) -> tuple[int, dict]:
    record = _open_record(payload)
    if record["state"] == "complete":
        return 200, {"complete": True}
    steps = qn.applicable_steps(Q, record["answers"])
    key = record["progress"]["next_step"] or steps[-1]["key"]
    return 200, {
        **_status(record),
        "complete": False,
        **_view(record, qn.step_by_key(Q, key)),
    }


ACTIONS = {
    "open": act_open,
    "start": act_start,
    "save": act_save,
    "load": act_load,
    "resume": act_resume,
}


# ----------------------------------------------------------------------------------- handler
def _method(event: dict) -> str:
    return (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "GET"
    ).upper()


def handler(event, context):
    method = _method(event)
    if method == "OPTIONS":
        return _resp(200, {})
    if method != "POST":
        return _resp(
            200, {"ok": True, "service": "meetrudi-internal-prod-prospect-intake"}
        )

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _resp(413, {"error": "payload_too_large"})
    try:
        payload = json.loads(raw)
    except ValueError:
        return _resp(400, {"error": "bad_json"})
    if not isinstance(payload, dict) or payload.get("action") not in ACTIONS:
        return _resp(400, {"error": "bad_request"})

    try:
        status, body = ACTIONS[payload["action"]](payload)
    except qn.Invalid as e:
        return _resp(422, {"error": "invalid", "field": e.field, "detail": e.detail})
    except Refused as e:
        return _resp(e.status, e.body)
    return _resp(status, body)
