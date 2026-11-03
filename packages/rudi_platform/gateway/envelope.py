"""
MEET_RUDI platform — the {reply, signals} envelope a JSON-mode generation returns.

Ported from responder._parse_envelope (services/whatsapp). Defensive on purpose: a model that
wraps its JSON in prose, or answers in prose outright, still yields a reply. `parsed` says which
happened, because a reply without signals silently stalls a journey (catalogue gap
`conversation-phases`) and the engine should be able to see it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Envelope:
    reply: str
    signals: dict[str, Any] = field(default_factory=dict)
    parsed: bool = False


def parse_envelope(text: str) -> Envelope:
    text = text or ""
    obj: Any
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        obj = None
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(text[start : end + 1])
            except (ValueError, TypeError):
                obj = None
    if not isinstance(obj, dict):
        return Envelope(reply=text)
    reply = obj.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = text
    signals = obj.get("signals") if isinstance(obj.get("signals"), dict) else {}
    return Envelope(reply=reply, signals=dict(signals), parsed=True)
