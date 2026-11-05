"""
MEET_RUDI platform — calls on the Turn Engine: what the voice adapter (edge-voice) calls.

Today's call service (services/call/src/ws.py) does three things around `brain.py` on every
utterance: scrub the transcript, build the model-safe call config (`_model_config`), run the
brain. These are those three, on the platform:

    open_call(engine, config, vault)                 the greeting when the person picks up
    call_turn(engine, state, transcript, config, …)  one utterance → reply

`config` is the call record's config as the dispatcher stores it: user_name, topic, notes,
language, timezone, call_goal, start_phase, max_minutes. The person's name never reaches the
model — it becomes an alias from the call's vault, restored only in the text that is spoken —
and the topic and notes are scrubbed like anything they say. The call's state is the engine's
`ConversationState` (flat form: `to_flat(state, clone.memory_schema("voice"))`); the vault lives
only as long as the call.

Telephony stays in the adapter: voicemail screening, hang-ups, the lost-thread and
operational-pause lines, the call ledger.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Any

from ..memory.state import ConversationState
from ..privacy.deid import AliasVault, placeholder, scrub_inbound
from .turn import TurnEngine, TurnOutcome

CHANNEL = "voice"


def model_call_config(
    config: Mapping[str, Any], vault: AliasVault, engine: TurnEngine
) -> dict[str, Any]:
    """The call config as the model may see it (port of ws._model_config)."""
    safe = dict(config)
    name = str(config.get("user_name") or "").strip()
    if name:
        safe["user_name"] = placeholder(vault.alias_for(name))
    language = str(config.get("language") or "en")
    for key in ("topic", "notes"):
        if config.get(key):
            safe[key] = scrub_inbound(
                vault, str(config[key]), engine.detector, language
            )[0]
    return safe


def _context(
    config: Mapping[str, Any], vault: AliasVault, engine: TurnEngine, elapsed_s: int
) -> dict[str, Any]:
    return {
        "call": model_call_config(config, vault, engine),
        "elapsed_s": int(elapsed_s or 0),
    }


def open_call(
    engine: TurnEngine,
    config: Mapping[str, Any],
    vault: AliasVault,
    now: datetime.datetime | None = None,
    persona_key: str = "",
) -> TurnOutcome:
    """The person has just picked up: a short model-written greeting from the call's start stage
    (`start_phase`, else the journey's voice start)."""
    return engine.open(
        channel=CHANNEL,
        now=now,
        persona_key=persona_key,
        vault=vault,
        locale=str(config.get("language") or "en"),
        tz=str(config.get("timezone") or ""),
        context=_context(config, vault, engine, 0),
        start_stage=str(config.get("start_phase") or "").strip() or None,
    )


def call_turn(
    engine: TurnEngine,
    state: ConversationState,
    transcript: str,
    config: Mapping[str, Any],
    vault: AliasVault,
    elapsed_s: int = 0,
    now: datetime.datetime | None = None,
    persona_key: str = "",
) -> TurnOutcome:
    """One finished utterance. `outcome.ended` means: say the reply, then hang up."""
    language = str(config.get("language") or "en")
    # The transcript is scrubbed before the call config is rebuilt, as on the live path, so the
    # aliases are handed out in the same order. The engine's own scrub then finds nothing new.
    masked, found = scrub_inbound(vault, transcript, engine.detector, language)
    outcome = engine.turn(
        state,
        masked,
        vault=vault,
        channel=CHANNEL,
        locale=language,
        tz=str(config.get("timezone") or ""),
        now=now,
        persona_key=persona_key,
        context=_context(config, vault, engine, elapsed_s),
    )
    outcome.masked_inbound, outcome.found = masked, found
    return outcome
