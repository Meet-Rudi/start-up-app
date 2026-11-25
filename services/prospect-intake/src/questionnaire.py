"""
The adoption-request questionnaire: its structure, conditions, validation and progress.

The questions are data (questionnaire.json), held on the server and handed to the browser one
step at a time, so the public page carries only the first step (the contact details) and a
generic renderer. That keeps the full questionnaire off the public page, lets it grow and branch
without a page release, and gives the browser and the server one definition to validate against.

A field's `show_if` names fields asked *earlier*: in the same step (the browser shows or hides the
field live) or in an earlier step (the server resolves it before it sends the step). A step may
carry a `show_if` on earlier steps too, which is how the questionnaire branches by organisation.

Pure functions, standard library only; the handler does the storage.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "questionnaire.json")

TEXT_TYPES = {"text", "email", "tel", "url", "textarea"}
CHOICE_TYPES = {"select", "radio", "checkbox"}
VALUE_TYPES = TEXT_TYPES | CHOICE_TYPES | {"scale", "time_window", "agree", "check"}
ALL_TYPES = VALUE_TYPES | {"note", "group"}
# The contact step is always first; the follow-up block of a stored request reads these keys.
CONTACT_KEYS = (
    "organisation",
    "contact_name",
    "contact_role",
    "contact_email",
    "contact_phone",
    "country",
)
# The icons the page can draw in a step's header (its ICONS map, web/adoption-request). A step
# names one of these; a new icon is drawn in the page first, then listed here.
ICONS = frozenset(
    {
        "id-card",
        "users",
        "message-circle-more",
        "list-checks",
        "smile",
        "sliders-horizontal",
        "gauge",
        "send",
        "target",
        "shield-check",
        "chart-column",
    }
)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_STEP_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://[^\s/]+\.[^\s]+$", re.IGNORECASE)
_TEL_RE = re.compile(r"^\+?[0-9 ().\-/]{6,30}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Invalid(Exception):
    """A submitted answer that the questionnaire refuses."""

    def __init__(self, field: str, detail: str):
        super().__init__(f"{field}: {detail}")
        self.field = field
        self.detail = detail


# ------------------------------------------------------------------------------- structure
def load(path: str = DEFAULT_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def fields_of(step: dict) -> list[dict]:
    """Every field of a step in order, a group's fields in place of the group."""
    out: list[dict] = []
    for f in step["fields"]:
        if f["type"] == "group":
            out.extend(f["fields"])
        else:
            out.append(f)
    return out


def value_fields(step: dict) -> list[dict]:
    return [f for f in fields_of(step) if f["type"] in VALUE_TYPES]


def step_by_key(q: dict, key: str) -> dict | None:
    return next((s for s in q["steps"] if s["key"] == key), None)


def _refs(cond: dict) -> list[str]:
    if "any" in cond:
        return [r for c in cond["any"] for r in _refs(c)]
    if "all" in cond:
        return [r for c in cond["all"] for r in _refs(c)]
    return [cond["field"]]


def check(q: dict) -> None:
    """Refuse a questionnaire the renderer or the validator could misread. Raises ValueError."""

    def fail(msg: str) -> None:
        raise ValueError(f"questionnaire: {msg}")

    if not q.get("questionnaire") or not q.get("form_version"):
        fail("needs `questionnaire` and `form_version`")
    steps = q.get("steps") or []
    if not steps or steps[0]["key"] != "contact":
        fail("the first step must be `contact`")
    first_keys = {f["key"] for f in fields_of(steps[0])}
    for k in CONTACT_KEYS:
        if k not in first_keys:
            fail(f"the contact step lacks `{k}`")
    if not any(
        f["key"] == "contact_email" and f.get("required") for f in fields_of(steps[0])
    ):
        fail("the contact e-mail must be required")

    seen_steps: set[str] = set()
    earlier: set[str] = set()  # fields of earlier steps
    for step in steps:
        if not _STEP_RE.match(step.get("key", "")) or step["key"] in seen_steps:
            fail(f"bad or repeated step key {step.get('key')!r}")
        seen_steps.add(step["key"])
        if not step.get("title"):
            fail(f"step {step['key']} has no title")
        if step.get("icon") not in ICONS:
            fail(
                f"step {step['key']}: icon {step.get('icon')!r} is not one the page draws"
            )
        if step.get("show_if") and not set(_refs(step["show_if"])) <= earlier:
            fail(f"step {step['key']}: show_if may name earlier steps' fields only")
        in_step: set[str] = set()
        for f in step["fields"]:
            if f.get("type") == "group":
                if any(g.get("type") == "group" for g in f.get("fields") or []):
                    fail(f"group {f.get('key')} holds a group")
                if not f.get("fields"):
                    fail(f"group {f.get('key')} is empty")
                if f.get("show_if"):
                    fail(f"group {f.get('key')}: put show_if on its fields")
        for f in fields_of(step):
            key, kind = f.get("key", ""), f.get("type")
            if not _KEY_RE.match(key) or key in earlier or key in in_step:
                fail(f"bad or repeated field key {key!r}")
            if kind not in ALL_TYPES:
                fail(f"{key}: unknown type {kind!r}")
            if kind not in {"note"} and not f.get("label"):
                fail(f"{key}: no label")
            if kind in CHOICE_TYPES:
                values = [o.get("value") for o in f.get("options") or []]
                if (
                    not values
                    or len(values) != len(set(values))
                    or not all(isinstance(v, str) and v for v in values)
                ):
                    fail(f"{key}: options need unique, non-empty values")
            if kind != "checkbox" and any(
                o.get("locked") for o in f.get("options") or []
            ):
                fail(f"{key}: only checkbox options can be locked")
            if kind == "scale" and not (
                isinstance(f.get("min"), int)
                and isinstance(f.get("max"), int)
                and f["min"] < f["max"]
                and len(f.get("ends") or []) == 2
            ):
                fail(f"{key}: a scale needs min < max and two ends")
            if f.get("show_if"):
                refs = set(_refs(f["show_if"]))
                if not refs <= (earlier | in_step):
                    fail(f"{key}: show_if may name only fields asked before it")
                if refs & in_step and refs & earlier:
                    fail(f"{key}: show_if mixes this step's and earlier steps' fields")
            if "default" in f:
                try:
                    clean_value(f, f["default"])
                except Invalid as e:
                    fail(f"{key}: default refused ({e.detail})")
            in_step.add(key)
        earlier |= in_step


# ------------------------------------------------------------------------------ conditions
def met(cond: dict | None, values: dict) -> bool:
    if not cond:
        return True
    if "any" in cond:
        return any(met(c, values) for c in cond["any"])
    if "all" in cond:
        return all(met(c, values) for c in cond["all"])
    v = values.get(cond["field"])
    if "is" in cond:
        return v == cond["is"]
    if "has" in cond:
        return isinstance(v, list) and cond["has"] in v
    if "in" in cond:
        return v in cond["in"]
    return False


def flatten(answers: dict) -> dict:
    """All stored answers, keyed by field (field keys are unique across the questionnaire)."""
    out: dict = {}
    for step_answers in answers.values():
        if isinstance(step_answers, dict):
            out.update(step_answers)
    return out


def applicable_steps(q: dict, answers: dict) -> list[dict]:
    values = flatten(answers)
    return [s for s in q["steps"] if met(s.get("show_if"), values)]


# ------------------------------------------------------------------------------ validation
def _text(f: dict, raw: Any) -> str:
    if not isinstance(raw, str):
        raise Invalid(f["key"], "not_text")
    value = _CONTROL_RE.sub("", raw).strip()
    if f["type"] != "textarea":
        value = " ".join(value.split())
    limit = f.get("max_len", 2000 if f["type"] == "textarea" else 200)
    if len(value) > limit:
        raise Invalid(f["key"], "too_long")
    if value and len(value) < f.get("min_len", 0):
        raise Invalid(f["key"], "too_short")
    if value and f["type"] == "email" and not _EMAIL_RE.match(value):
        raise Invalid(f["key"], "not_an_email")
    if value and f["type"] == "url" and not _URL_RE.match(value):
        raise Invalid(f["key"], "not_a_url")
    if value and f["type"] == "tel" and not _TEL_RE.match(value):
        raise Invalid(f["key"], "not_a_phone")
    return value


def clean_value(f: dict, raw: Any) -> Any:
    """The stored form of one answer, or None when it is empty. Raises Invalid."""
    kind, key = f["type"], f["key"]
    if raw is None or raw == "" or raw == []:
        return None
    if kind in TEXT_TYPES:
        return _text(f, raw) or None
    options = [o["value"] for o in f.get("options") or []]
    if kind in ("select", "radio"):
        if raw not in options:
            raise Invalid(key, "not_an_option")
        return raw
    if kind == "checkbox":
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise Invalid(key, "not_a_list")
        if len(raw) != len(set(raw)) or any(v not in options for v in raw):
            raise Invalid(key, "not_an_option")
        return [v for v in options if v in raw]  # questionnaire order
    if kind == "scale":
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or not f["min"] <= raw <= f["max"]
        ):
            raise Invalid(key, "out_of_range")
        return raw
    if kind == "time_window":
        if not isinstance(raw, dict):
            raise Invalid(key, "not_a_window")
        start, end = raw.get("from"), raw.get("to")
        if not (
            isinstance(start, str)
            and isinstance(end, str)
            and _TIME_RE.match(start)
            and _TIME_RE.match(end)
        ):
            raise Invalid(key, "not_a_time")
        if start >= end:
            raise Invalid(key, "ends_before_start")
        return {"from": start, "to": end}
    if kind in ("agree", "check"):
        if not isinstance(raw, bool):
            raise Invalid(key, "not_a_tick")
        return raw if kind == "check" else (True if raw else None)
    raise Invalid(key, "no_value")


def _missing(f: dict, value: Any) -> bool:
    if f["type"] == "checkbox":
        need = f.get("min_items", 1 if f.get("required") else 0)
        return len(value or []) < need
    return f.get("required", False) and value is None


def clean_step(step: dict, submitted: Any, earlier_values: dict, draft: bool) -> dict:
    """Validate one step's answers. `earlier_values` are the stored answers of other steps.

    Fields are read in order, so a field whose condition is not met is dropped, and a later
    field's condition sees only what is actually shown. A draft keeps whatever is filled in and
    skips the required checks; a full save refuses a missing answer. Raises Invalid.
    """
    if not isinstance(submitted, dict):
        raise Invalid("answers", "not_an_object")
    values = dict(earlier_values)
    out: dict = {}
    for f in value_fields(step):
        key = f["key"]
        if not met(f.get("show_if"), values):
            values.pop(key, None)
            continue
        value = clean_value(f, submitted.get(key))
        locked = [o["value"] for o in f.get("options") or [] if o.get("locked")]
        if locked:
            value = [
                o["value"]
                for o in f["options"]
                if o["value"] in set(value or []) | set(locked)
            ]
        if not draft and _missing(f, value):
            raise Invalid(key, "required")
        if value is not None:
            out[key] = value
            values[key] = value
        else:
            values.pop(key, None)
    return out


# -------------------------------------------------------------------------------- progress
def step_done(step: dict, answers: dict) -> bool:
    """True when every shown, required answer of `step` is stored."""
    if step["key"] not in answers:
        return False
    mine = answers.get(step["key"]) or {}
    shown = {k: v for k, v in flatten(answers).items() if k not in mine}
    for f in value_fields(step):
        if not met(f.get("show_if"), shown):
            continue
        value = mine.get(f["key"])
        if _missing(f, value):
            return False
        if value is not None:
            shown[f["key"]] = value
    return True


def progress(q: dict, answers: dict) -> dict:
    steps = applicable_steps(q, answers)
    done = [s["key"] for s in steps if step_done(s, answers)]
    open_steps = [s["key"] for s in steps if s["key"] not in done]
    return {
        "done": done,
        "total": len(steps),
        "percent": round(100 * len(done) / len(steps)) if steps else 0,
        "next_step": open_steps[0] if open_steps else None,
    }


def steps_summary(q: dict, answers: dict) -> list[dict]:
    done = set(progress(q, answers)["done"])
    return [
        {"key": s["key"], "title": s["title"], "done": s["key"] in done}
        for s in applicable_steps(q, answers)
    ]


# ------------------------------------------------------------------- answers, as people read them
NO_ANSWER = "—"


def display_value(f: dict, value: Any) -> str:
    """One stored answer in words: option labels, not their codes."""
    if value is None or value == "" or value == []:
        return NO_ANSWER
    kind = f["type"]
    labels = {o["value"]: o["label"] for o in f.get("options") or []}
    if kind in ("select", "radio"):
        return labels.get(value, str(value))
    if kind == "checkbox":
        return ", ".join(labels.get(v, v) for v in value)
    if kind == "scale":
        return f"{value} of {f['max']} ({f['ends'][0]} to {f['ends'][1]})"
    if kind == "time_window":
        return f"{value['from']} to {value['to']}"
    if kind == "agree":
        return "Agreed" if value is True else NO_ANSWER
    if kind == "check":
        return "Yes" if value else "No"
    return str(value)


def answer_table(q: dict, answers: dict, steps: tuple[str, ...] = ()) -> list[dict]:
    """Each applicable step (or only `steps`) with the label and answer of every field that was
    shown, in questionnaire order. An unanswered optional question reads NO_ANSWER."""
    out: list[dict] = []
    shown: dict = {}
    for number, step in enumerate(applicable_steps(q, answers), start=1):
        mine = answers.get(step["key"]) or {}
        rows = []
        for f in value_fields(step):
            if not met(f.get("show_if"), shown):
                continue
            value = mine.get(f["key"])
            if value is not None:
                shown[f["key"]] = value
            rows.append({"label": f["label"], "value": display_value(f, value)})
        if not steps or step["key"] in steps:
            out.append(
                {
                    "key": step["key"],
                    "number": number,
                    "title": step["title"],
                    "rows": rows,
                }
            )
    return out


# --------------------------------------------------------------------- what the browser gets
def _resolve(f: dict, values: dict, in_step: set[str]) -> dict | None:
    """The field as the browser gets it: a condition on earlier steps is settled here."""
    cond = f.get("show_if")
    if not cond or set(_refs(cond)) & in_step:
        return dict(f)
    if not met(cond, values):
        return None
    out = dict(f)
    out.pop("show_if")
    return out


def public_step(q: dict, step: dict, answers: dict) -> dict:
    """One step for the browser: its fields, with conditions on earlier steps settled."""
    values = flatten({k: v for k, v in answers.items() if k != step["key"]})
    in_step = {f["key"] for f in fields_of(step)}
    fields = []
    for f in step["fields"]:
        if f["type"] == "group":
            inner = [
                g for g in (_resolve(x, values, in_step) for x in f["fields"]) if g
            ]
            if inner:
                fields.append({**f, "fields": inner})
            continue
        resolved = _resolve(f, values, in_step)
        if resolved:
            fields.append(resolved)
    steps = applicable_steps(q, answers)
    position = next((i for i, s in enumerate(steps) if s["key"] == step["key"]), 0)
    return {
        "key": step["key"],
        "title": step["title"],
        "icon": step["icon"],
        "intro": step.get("intro", ""),
        "number": position + 1,
        "total": len(steps),
        "last": position == len(steps) - 1,
        "fields": fields,
    }
