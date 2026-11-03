"""
MEET_RUDI platform — reply leak guard (CLAUDE.md §6: a generation that fails the gate is not sent).

Ported from services/whatsapp/src/leakguard.py on 2026-10-28; the WhatsApp and call services keep
their own identical copies until they are cut over (conventions §6).

A prompt injection ("print your instructions") works by getting the model to recite what it was
given. This guard makes sure a model that does so anyway is not believed. Deterministic, no model
call, microseconds:

  1. INTERNAL MARKERS  strings that exist only inside our prompts. No honest reply contains one.
  2. VERBATIM COPY     OVERLAP_WORDS consecutive words lifted from the system prompt. Quoted spans
                       (the person's own words) and the public "About me" context are cut first.

Platform change: markers are extensible. MARKERS is the platform and coaching-pack set; each
skill adds the internal headings and signal names its prompt sections introduce through
`extra_markers`, so a new skill's internals are protected without editing this file.

A blocked reply is replaced by a short localised redirect and logged by reason only, never by
content.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping

log = logging.getLogger(__name__)

OVERLAP_WORDS = 10

# Lower-case, accent-free, whitespace-collapsed: the form check() compares against.
MARKERS = (
    # runtime notes and briefs appended to prompts
    "[runtime:", "[channel:", "[language:", "[checking back:", "[now:", "[time marker",
    "[context]", "# call brief", "# speaking, not writing", "# about me (context)",
    "# coming back to them", "# handing over to whatsapp", "# you have not yet told them",
    "# health & wellness coaching guidance", "# who you are", "your personality profile:",
    # the guardrails file
    "non-negotiable rules", "# confidentiality",
    # output contract and signal names meant for code
    "respond only as a single json", "check_in_minutes", "check_in_about", "want_to_try",
    "goal_status", "commitment_made", "goal_domain", "clarifiers_left", "attempts_left",
    "reject_attempts_left",
    # internal call goals and config
    "call_goal", "start_phase", "get_to_know", "set_nearterm_goal", "goal_followup",
    "reinstate_talk",
    # the business context document
    "internal source of truth", "website chatbot prompt", "source pages (internal)",
)  # fmt: skip

SAFE_REPLY = {
    "en": "Let's keep the focus on you. How are things going with what you're working on?",
    "nl": "Laten we de focus bij jou houden. Hoe gaat het met waar je mee bezig bent?",
    "fr": "Restons concentrés sur toi. Comment ça avance de ton côté ?",
    "de": "Lass uns bei dir bleiben. Wie läuft es mit dem, woran du gerade arbeitest?",
}

_QUOTED = re.compile(r'"[^"\n]{0,400}"|“[^”\n]{0,400}”')
_CONTEXT = re.compile(
    r"# About me \(context\).*?(?=\n\n# Speaking, not writing|\n\n\[|\Z)", re.DOTALL
)
_WORD = re.compile(r"[a-z0-9]+")


def norm(text: object) -> str:
    """The comparison form: NFKD, lower-case, accents removed."""
    s = unicodedata.normalize("NFKD", str(text or "")).lower()
    return "".join(c for c in s if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return _WORD.findall(norm(text))


def check(
    reply: str, system_prompt: str = "", extra_markers: Iterable[str] = ()
) -> str:
    """Why this reply must not be sent: "marker", "verbatim", or "" when it may go."""
    low = " ".join(norm(reply).split())
    for marker in (*MARKERS, *(norm(m) for m in extra_markers)):
        if marker and marker in low:
            return "marker"
    if system_prompt:
        out = _words(reply)
        src = _words(_QUOTED.sub(" ", _CONTEXT.sub(" ", system_prompt)))
        n = OVERLAP_WORDS
        if len(out) >= n and len(src) >= n:
            grams = {tuple(src[i : i + n]) for i in range(len(src) - n + 1)}
            for i in range(len(out) - n + 1):
                if tuple(out[i : i + n]) in grams:
                    return "verbatim"
    return ""


def safe_reply(lang: str = "en", replies: Mapping[str, str] | None = None) -> str:
    """The redirect in the person's language; a clone may supply its own wording."""
    table = replies or SAFE_REPLY
    code = str(lang or "en").strip().lower().replace("_", "-").split("-")[0]
    return table.get(code) or table.get("en") or SAFE_REPLY["en"]


def guard(
    reply: str,
    system_prompt: str = "",
    lang: str = "en",
    extra_markers: Iterable[str] = (),
    replies: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """(text_to_send, reason). An empty reason means the reply passed untouched."""
    reason = check(reply, system_prompt, extra_markers)
    if not reason:
        return reply, ""
    log.warning("leakguard blocked reason=%s", reason)
    return safe_reply(lang, replies), reason
