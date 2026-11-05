"""
MEET_RUDI platform — assembling the system prompt from the parts skills contribute.

Each real-time skill's `contribute` hook returns parts: `{"slot", "text", "order"}`. The engine
renders the slots in this fixed order, so safety comes first and the clock last:

    rules       "# {persona} — non-negotiable rules" + one line per part (Baseline and coaching
                rules, interleaved by `order`)
    policies    further non-negotiable blocks (confidentiality, acceptable use)
    persona     temperament (OCEAN)
    style       how to speak on this channel (a call: short spoken sentences)
    stage       the journey stage's instructions and knowledge
    call        the call brief: who, why, the AI disclosure, the call's goal
    stage_note  the stage's runtime note (counters, the goal, the commitment, the farewell)
    budget      time pressure on a live call
    channel     how this channel works (the channel adapter's brief)
    language    language mirroring
    check_in    promising the next contact
    time        the clock

A WhatsApp turn fills rules, policies, persona, stage, stage_note, channel, language, check_in,
time; a call fills rules, policies, style, stage, call, stage_note, budget, time. Both orders are
today's live prompts.

Within a slot, parts are ordered by `order`, then by composition order; `rules` parts are joined
by a single newline (they are list items), every other join is a blank line.

Placeholders: prompt text may use `{persona}` and the skill's own values. Only `{name}` tokens
whose name is supplied are replaced, so JSON examples such as `{"reply": "..."}` pass untouched.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SLOTS = (
    "rules",
    "policies",
    "persona",
    "style",
    "stage",
    "call",
    "stage_note",
    "budget",
    "channel",
    "language",
    "check_in",
    "time",
)
RULES_HEADER = "# {persona} — non-negotiable rules (never break these)"
_TOKEN = re.compile(r"\{([a-z][a-z0-9_]*)\}")


class PromptError(ValueError):
    """A skill contributed a part the engine cannot place."""


@dataclass(frozen=True)
class PromptPart:
    slot: str
    text: str
    order: int = 50

    @classmethod
    def from_dict(cls, d: Mapping[str, object], who: str) -> PromptPart:
        slot, text, order = d.get("slot"), d.get("text"), d.get("order", 50)
        if slot not in SLOTS:
            raise PromptError(f"{who}: unknown prompt slot {slot!r}")
        if not isinstance(text, str):
            raise PromptError(f"{who}: prompt text must be a string")
        if not isinstance(order, int) or isinstance(order, bool):
            raise PromptError(f"{who}: prompt order must be an integer")
        return cls(str(slot), text, order)


def fill(text: str, values: Mapping[str, object]) -> str:
    """Replace `{name}` for every name in `values`; leave every other brace alone."""

    def swap(m: re.Match[str]) -> str:
        name = m.group(1)
        return str(values[name]) if name in values else m.group(0)

    return _TOKEN.sub(swap, text)


def render_system(parts: Iterable[PromptPart], persona: str) -> str:
    """The system prompt. Empty parts are skipped; an empty slot leaves no gap."""
    by_slot: dict[str, list[tuple[int, int, str]]] = {s: [] for s in SLOTS}
    for i, part in enumerate(parts):
        text = part.text.strip("\n")
        if text.strip():
            by_slot[part.slot].append((part.order, i, text))
    blocks: list[str] = []
    for slot in SLOTS:
        texts = [t for _, _, t in sorted(by_slot[slot])]
        if not texts:
            continue
        if slot == "rules":
            blocks.append(
                fill(RULES_HEADER, {"persona": persona}) + "\n\n" + "\n".join(texts)
            )
        else:
            blocks.append("\n\n".join(texts))
    return "\n\n".join(blocks)


def canonical(text: str) -> str:
    """Line endings and blank-line runs normalised: the form two prompts are compared in."""
    out = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()
