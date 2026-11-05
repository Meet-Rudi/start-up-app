"""
MEET_RUDI platform — what the engine needs to know about each channel (blueprint §3, §9).

The channel adapters own delivery, windows, templates, telephony. The engine needs from a channel:
the brief that tells the model where it is talking (catalogue `chat-message-style`, placed with
the channel adapter by the v0.1 review), how a reply is formatted for it, its time budget, and
how a conversation opens and ends on it.

  - WhatsApp: today's `responder.CHANNEL` brief (with `{persona}`), **bold** → *bold*; a canned
    greeting opens; a concluded session restarts with a welcome-back.
  - Voice: today's `services/call` brain — the model writes the opening greeting (the person has
    just picked up), replies are cleaned for speech and stored as spoken, the turn has 18 s and
    each attempt 7 s, a reply may use 1024 tokens, thinking included, and a concluded call is
    over (the adapter hangs up; it never restarts).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

WHATSAPP_BRIEF = (
    "[Channel: You are {persona} talking with the person on WhatsApp — via text, voice notes, "
    "and photos/videos they share (voice calls later). You are NOT a website chatbot. If "
    "asked how you communicate or stay in touch, say it's here on WhatsApp; never claim "
    "you'll switch to a website/app or that you can't continue here. If they send a photo "
    "or voice note you can't process yet, acknowledge it warmly and say you'll be able to "
    "look/listen properly soon.]"
)

# The user turn of a call's opening generation: nobody has spoken yet.
VOICE_OPENING_CUE = (
    "(system: they have just picked up and said nothing yet — greet them)"
)

_ALIAS = re.compile(r"<\s*\|\s*[A-Za-z0-9_\-]{1,40}\s*\|\s*>")
# An alias the model wrote without its brackets ("Hi Person_A", "Hi Person A"). Smaller fallback
# models do this (seen on calls, 2026-11-04); the caller then heard "Person A" instead of a name.
_BARE_ALIAS = re.compile(r"\bPerson(?:_([A-Z]{1,3})|\s([A-Z]))\b")


def whatsapp_style(text: str) -> str:
    """Light markdown → WhatsApp: **bold** → *bold* (WhatsApp bold is a single asterisk)."""
    return (text or "").replace("**", "*")


def speakable(text: str) -> str:
    """Strip what a synthesiser would read aloud as punctuation noise (port of brain._speakable).

    Aliases are set aside first: turning "_" into a space would make "<| Person_A |>" into
    "<| Person A |>", which the vault no longer recognises, and a raw placeholder would be spoken.
    A bare alias is put back into placeholder form, so the name is restored rather than read out.
    """
    held: list[str] = []

    def hold(m: re.Match[str]) -> str:
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"

    def hold_bare(m: re.Match[str]) -> str:
        held.append(f"<| Person_{m.group(1) or m.group(2)} |>")
        return f"\x00{len(held) - 1}\x00"

    out = _ALIAS.sub(hold, text or "")
    out = _BARE_ALIAS.sub(hold_bare, out)
    out = out.replace("**", "").replace("*", "").replace("#", "")
    out = out.replace("`", "").replace("_", " ")
    out = " ".join(out.split())
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], out)


def _plain(text: str) -> str:
    return text or ""


@dataclass(frozen=True)
class ChannelProfile:
    name: str
    brief: str = ""
    style: Callable[[str], str] = field(default=_plain)
    # Time budget for one turn's model call, and for each attempt in it; None = messaging pace.
    budget_s: float | None = None
    attempt_timeout_s: float | None = None
    max_tokens: int | None = None
    max_history: int = 20  # history messages sent to the model per turn
    # True: the history keeps the reply as delivered (on a call, what was said is what was spoken).
    store_styled: bool = False
    # Set: the model writes the opening, with this as the user turn. None: a canned greeting.
    opening_cue: str | None = None
    # False: a concluded conversation is over on this channel (a call), not restarted.
    restart_after_conclusion: bool = True
    # True: a turn's answer without the {reply, signals} envelope fails over to the next model
    # instead of being used (prose is read as the reply, and internal field names in it trip the
    # leak guard). On a call the redirect replaced the AI-disclosure turn (2026-11-05). Off on
    # WhatsApp, where legacy still sends such prose as the reply (decision #36 parity).
    require_reply: bool = False


PROFILES: dict[str, ChannelProfile] = {
    "whatsapp": ChannelProfile("whatsapp", WHATSAPP_BRIEF, whatsapp_style),
    "sms": ChannelProfile("sms"),
    "email": ChannelProfile("email"),
    "telegram": ChannelProfile("telegram"),
    "api": ChannelProfile("api", budget_s=8.0),
    "widget": ChannelProfile("widget", budget_s=8.0),
    "voice": ChannelProfile(
        "voice",
        style=speakable,
        budget_s=18.0,
        attempt_timeout_s=7.0,
        # gpt-oss thinks before it answers and the thinking counts against this cap: at 250 it
        # used the lot and returned nothing on every turn (2026-11-04). A spoken reply is ~50.
        max_tokens=1024,
        max_history=24,
        store_styled=True,
        opening_cue=VOICE_OPENING_CUE,
        restart_after_conclusion=False,
        require_reply=True,
    ),
}


def profile(channel: str) -> ChannelProfile:
    try:
        return PROFILES[channel]
    except KeyError:
        raise ValueError(f"no channel profile for {channel!r}") from None
