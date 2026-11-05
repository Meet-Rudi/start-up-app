"""
MEET_RUDI platform — the state of one conversation, as the engine reads and writes it.

    stage    the journey stage it is in; None = never spoken; "concluded" = session over
    session  how many sessions it has opened
    history  the model history of this session: [{role, content, at}], de-identified
    memory   the conversation's memory fields, typed by the clone's memory schema (the union of
             its skills' manifest `memory.writes`)
    extra    keys an older writer left in the stored form; carried through untouched

The **flat form** is the one CM's legacy store keeps as `meta.json → ai_state`:

    {"phase": "commit", "session_id": 2, "history": [...],
     "goal": "...", "goal_domain": "fitness", "commit_attempts": 1, ...}

Memory fields sit at the top level, and every field of the schema is present: numbers default to
0, true/false to false, everything else to null. That is exactly what the legacy responder
writes, so a conversation can be served by the legacy engine on one turn and the platform engine
on the next, in both directions, during the cut-over (conventions §6). A conversation that has
never spoken is `{}`.

The schema is per channel (`ComposedClone.memory_schema_for`): a WhatsApp thread's stored state
holds the fields of the skills that run on WhatsApp, a call's those of the skills on calls.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

RESERVED = frozenset({"phase", "session_id", "history"})
NUMERIC = frozenset({"int", "float"})


@dataclass
class ConversationState:
    stage: str | None = None
    session: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_new(self) -> bool:
        return self.stage is None

    def copy(self) -> ConversationState:
        return copy.deepcopy(self)


def default_for(type_name: str) -> Any:
    """Numbers 0, true/false false, everything else null — what the legacy writers store."""
    if type_name == "bool":
        return False
    return 0 if type_name in NUMERIC else None


def to_flat(state: ConversationState, schema: Mapping[str, str]) -> dict[str, Any]:
    """The stored (legacy `ai_state`) form of `state`."""
    if state.is_new:
        return {}
    out: dict[str, Any] = {
        "phase": state.stage,
        "session_id": state.session,
        "history": copy.deepcopy(state.history),
    }
    for name, type_name in schema.items():
        out[name] = copy.deepcopy(state.memory.get(name, default_for(type_name)))
    # A field the schema no longer lists survives.
    for name, value in state.memory.items():
        out.setdefault(name, copy.deepcopy(value))
    for name, value in state.extra.items():
        out.setdefault(name, copy.deepcopy(value))
    return out


def from_flat(
    flat: Mapping[str, Any] | None, schema: Mapping[str, str]
) -> ConversationState:
    """`to_flat` reversed. Unknown keys go to `extra`, so nothing a writer stored is lost."""
    if not flat:
        return ConversationState()
    history = flat.get("history") or []
    session = flat.get("session_id") or 0
    return ConversationState(
        # A stored state without a phase is a conversation whose session ended, not a stranger:
        # the legacy responder greets it as returning, and so must the engine.
        stage=flat.get("phase") or "concluded",
        session=session if isinstance(session, int) else 0,
        history=copy.deepcopy(list(history)) if isinstance(history, list) else [],
        memory={k: copy.deepcopy(v) for k, v in flat.items() if k in schema},
        extra={
            k: copy.deepcopy(v)
            for k, v in flat.items()
            if k not in schema and k not in RESERVED
        },
    )
