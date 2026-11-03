"""
MEET_RUDI platform — the skill envelope.

Every skill, of every kind, takes a `SkillRequest` and returns a `SkillResult` (decision #25).
A real-time skill gets them as Python objects inside the engine; a turn or background skill gets
them as a Lambda JSON payload (`to_payload` / `from_payload`). Because the shape is the same, a
skill can move between kinds without its callers changing.

`context` is optional on every request (blueprint H6): a JSON dictionary the skill may read and
patch. It is what a long-running skill hands to its successor, so it must stay serialisable.

Plain dataclasses on purpose: the Lambda bundles are zip packages built on Windows, and pydantic's
compiled core would need a Linux-targeted layer build for every function that imports this.
Validation is explicit in `validate()` instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from ..ids import Cat, require
from ..naming import CELL_RE

CHANNELS = (
    "whatsapp",
    "voice",
    "api",
    "widget",
    "sms",
    "email",
    "telegram",
    "arena",
    "system",
)
SKILL_REF_RE = re.compile(
    r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)@(?P<ver>\d+\.\d+\.\d+)$"
)


class SkillKind(str, Enum):
    REAL_TIME = "real_time"  # inside the engine process, every turn, no network hop
    TURN = "turn"  # own Lambda, called within the turn, skipped when late
    BACKGROUND = "background"  # own Lambda, after the reply or on a schedule


@dataclass(frozen=True)
class SkillRef:
    """A skill slug pinned to an exact version, written `goal-setting@1.4.2`."""

    slug: str
    version: tuple[int, int, int]

    @classmethod
    def parse(cls, text: str) -> SkillRef:
        m = SKILL_REF_RE.match(text or "")
        if not m:
            raise ValueError(
                f"skill reference must look like 'slug@1.2.3', got {text!r}"
            )
        major, minor, patch = (int(x) for x in m.group("ver").split("."))
        return cls(m.group("slug"), (major, minor, patch))

    @property
    def major(self) -> int:
        return self.version[0]

    def __str__(self) -> str:
        major, minor, patch = self.version
        return f"{self.slug}@{major}.{minor}.{patch}"


@dataclass(frozen=True)
class Usage:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_eur: float = 0.0


@dataclass(frozen=True)
class MemoryWrite:
    """One memory field the skill wants written. Only fields its manifest declares are accepted."""

    field: str
    value: Any


@dataclass(frozen=True)
class Continuation:
    """Set on a result when the skill handed its work to a successor run (runtime.handover)."""

    checkpoint: str
    job: str
    hop: int
    next_input: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillRequest:
    skill: SkillRef
    tenant: str
    channel: str
    input: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] | None = None
    clone: str | None = None
    clone_version: str | None = None
    cell: str | None = None
    end_user: str | None = None
    conversation: str | None = None
    turn: str | None = None
    deadline_ms: int | None = None
    job: str | None = None
    hop: int = 0

    def validate(self) -> SkillRequest:
        """Raise ValueError on the first malformed field; return self when all are sound."""
        require(self.tenant, Cat.TENANT)
        for value, cat in (
            (self.clone, Cat.CLONE),
            (self.clone_version, Cat.CLONE_VERSION),
            (self.end_user, Cat.USER),
            (self.conversation, Cat.CONVERSATION),
            (self.turn, Cat.TURN),
            (self.job, Cat.JOB),
        ):
            if value is not None:
                require(value, cat)
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        if self.cell is not None and not CELL_RE.match(self.cell):
            raise ValueError(f"malformed cell {self.cell!r}")
        if self.deadline_ms is not None and self.deadline_ms <= 0:
            raise ValueError("deadline_ms must be positive")
        if self.hop < 0:
            raise ValueError("hop must not be negative")
        if self.hop > 0 and self.job is None:
            raise ValueError("a continued run (hop > 0) must carry its job id")
        for name in ("input", "settings", "context"):
            value = getattr(self, name)
            if value is None and name == "context":
                continue
            if not isinstance(value, dict):
                # One exception type for every malformed payload, so handlers catch one thing.
                raise ValueError(f"{name} must be a dictionary")  # noqa: TRY004
            _require_json(value, name)
        return self

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "skill": str(self.skill),
            "tenant": self.tenant,
            "channel": self.channel,
            "input": self.input,
            "settings": self.settings,
            "context": self.context,
            "hop": self.hop,
        }
        for name in (
            "clone",
            "clone_version",
            "cell",
            "end_user",
            "conversation",
            "turn",
            "deadline_ms",
            "job",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SkillRequest:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dictionary")  # noqa: TRY004 (see validate)
        known = {
            "tenant",
            "channel",
            "input",
            "settings",
            "context",
            "clone",
            "clone_version",
            "cell",
            "end_user",
            "conversation",
            "turn",
            "deadline_ms",
            "job",
            "hop",
        }
        args = {k: v for k, v in payload.items() if k in known}
        args.setdefault("input", {})
        args.setdefault("settings", {})
        return cls(skill=SkillRef.parse(payload.get("skill", "")), **args).validate()

    def with_context(self, patch: dict[str, Any] | None) -> SkillRequest:
        """A copy whose context has `patch` merged in (see merge_context)."""
        return replace(self, context=merge_context(self.context, patch))


@dataclass(frozen=True)
class SkillResult:
    output: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)
    context_patch: dict[str, Any] = field(default_factory=dict)
    memory_writes: tuple[MemoryWrite, ...] = ()
    usage: tuple[Usage, ...] = ()
    continuation: Continuation | None = None

    def to_payload(self) -> dict[str, Any]:
        for name in ("output", "signals", "context_patch"):
            _require_json(getattr(self, name), name)
        payload: dict[str, Any] = {
            "output": self.output,
            "signals": self.signals,
            "context_patch": self.context_patch,
            "memory_writes": [
                {"field": w.field, "value": w.value} for w in self.memory_writes
            ],
            "usage": [u.__dict__ for u in self.usage],
            "continuation": None,
        }
        if self.continuation is not None:
            payload["continuation"] = dict(self.continuation.__dict__)
        _require_json(payload, "result")
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SkillResult:
        cont = payload.get("continuation")
        if cont is not None:
            require(cont.get("checkpoint"), Cat.CHECKPOINT)
            require(cont.get("job"), Cat.JOB)
        return cls(
            output=dict(payload.get("output") or {}),
            signals=dict(payload.get("signals") or {}),
            context_patch=dict(payload.get("context_patch") or {}),
            memory_writes=tuple(
                MemoryWrite(w["field"], w.get("value"))
                for w in payload.get("memory_writes") or ()
            ),
            usage=tuple(Usage(**u) for u in payload.get("usage") or ()),
            continuation=Continuation(**cont) if cont is not None else None,
        )


def merge_context(
    context: dict[str, Any] | None, patch: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Shallow merge: keys in `patch` replace those in `context`; a None value removes the key.

    Returns None only when there was no context and the patch adds nothing, so a skill that
    never used context keeps sending none.
    """
    if not patch:
        return context
    merged = dict(context or {})
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged if (merged or context is not None) else None


def _require_json(value: Any, name: str) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serialisable: {exc}") from None


__all__ = [
    "CHANNELS",
    "Continuation",
    "MemoryWrite",
    "SkillKind",
    "SkillRef",
    "SkillRequest",
    "SkillResult",
    "Usage",
    "merge_context",
]
