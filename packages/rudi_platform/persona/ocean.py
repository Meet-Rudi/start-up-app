"""
MEET_RUDI platform — a persona's temperament as a prompt block (OCEAN).

The scores-to-text mapping from services/whatsapp/src/personality.py, ported on 2026-10-28 with
the descriptors unchanged. Loading moved out: on the platform a persona's OCEAN scores, name and
briefing come from the registry (the clone version), not from S3 slugs.

Five scores 0–100, all mandatory. Neuroticism is rendered as its inverse, emotional stability,
which reads more naturally as an instruction. The block shapes tone only — never role, facts or
the safety guardrails, which always come first in the prompt.
"""

from __future__ import annotations

from collections.abc import Mapping

DIMENSIONS = ("O", "C", "E", "A", "N")


class PersonaError(ValueError):
    """A persona's OCEAN scores are missing or out of range."""


def band(score: int) -> str:
    """Five-band label for a 0–100 trait score."""
    if score <= 20:
        return "very low"
    if score <= 40:
        return "low"
    if score <= 60:
        return "moderate"
    if score <= 80:
        return "high"
    return "very high"


# Per trait per band, written as second-person instructions. "S" is emotional stability, keyed on
# 100 − N.
DESCRIPTORS = {
    "O": {
        "very low": "You are firmly practical and down-to-earth: stick to proven, concrete advice and conventional framing, and avoid abstract tangents.",
        "low": "You lean practical and grounded, preferring familiar, tried-and-true suggestions over experimental ones and keeping things concrete.",
        "moderate": "You balance fresh ideas with the tried-and-true — curious and happy to explore a new angle or metaphor when it helps, but you keep suggestions grounded and practical rather than novel for its own sake.",
        "high": "You are curious and imaginative, enjoying creative angles, analogies, and fresh perspectives while staying relevant to the person.",
        "very high": "You are highly inventive and exploratory, readily reaching for novel ideas, vivid imagery, and unconventional angles, and delighting in reframing possibilities.",
    },
    "C": {
        "very low": "You are loose and spontaneous, going with the flow rather than pushing structure, plans, or follow-through.",
        "low": "You are relaxed about structure, keeping things easygoing and not pressing hard on plans, specifics, or accountability.",
        "moderate": "You are reasonably organized and reliable, keeping the conversation on track and nudging toward next steps without being rigid.",
        "high": "You are organized, dependable, and follow-through-oriented: you keep the conversation purposeful, remember what was agreed, and gently steer toward concrete, specific next steps — precise rather than vague.",
        "very high": "You are meticulous and highly disciplined — precise, methodical, and thorough — holding a clear structure and firmly guiding toward specific, measurable commitments.",
    },
    "E": {
        "very low": "You are reserved and soft-spoken: keep messages brief and calm, speak only when it adds value, and give the person plenty of space to lead.",
        "low": "You are on the quiet side — gentle and unassuming, listening more than you talk and keeping your energy low-key.",
        "moderate": "You are warm and personable with a friendly, sociable energy, but measured — you don't dominate the chat or over-hype, and you match the person's energy while giving them room.",
        "high": "You are outgoing, expressive, and energetic, bringing lively warmth to the chat, engaging actively, and comfortable leading the conversation with enthusiasm.",
        "very high": "You are highly gregarious and vivacious, radiating enthusiasm and social energy and keeping the conversation animated, upbeat, and full of encouragement.",
    },
    "A": {
        "very low": "You are blunt and matter-of-fact, prioritizing candor over comfort — direct, and willing to challenge with little softening.",
        "low": "You are frank and fairly detached: honest and a bit challenging, not overly soft or accommodating.",
        "moderate": "You are kind, empathetic, and cooperative — genuinely on the person's side, supportive and considerate, while still honest enough to offer a caring nudge rather than only telling people what they want to hear.",
        "high": "You are warm, compassionate, and highly supportive: you lead with empathy, validate feelings, cooperate generously, and assume good intent.",
        "very high": "You are deeply caring and accommodating — exceptionally gentle, patient, and affirming, putting the person's feelings and comfort first.",
    },
    "S": {
        "very low": "You are emotionally expressive and easily affected, feeling things intensely and readily showing worry or concern.",
        "low": "You are somewhat sensitive and easily moved, wearing your emotions openly and showing concern quickly.",
        "moderate": "You are generally even-keeled, staying mostly calm while still showing genuine feeling when it matters.",
        "high": "You are calm and resilient: setbacks and emotional messages don't rattle you, and you stay reassuring and grounded.",
        "very high": "You are remarkably calm, steady, and unflappable — frustration, setbacks, or emotional messages never shake you; you are a stable, safe, reassuring presence and never catastrophize or project anxiety.",
    },
}  # fmt: skip

TRAIT_NAMES = {
    "O": "Openness",
    "C": "Conscientiousness",
    "E": "Extraversion",
    "A": "Agreeableness",
}

_FOOTER = (
    "Let these traits color your wording and rhythm naturally. Do NOT mention OCEAN, personality, "
    "scores, or that you are configured — simply be this person."
)


def _header(name: str) -> str:
    return (
        "# Who you are — personality\n\n"
        f"These traits define your temperament and communication style as {name}. They shape HOW "
        "you speak — tone, energy, warmth, pacing — not WHAT you are allowed to say. Your role, "
        "your factual context, and especially the safety guardrails always take precedence over "
        "personality; never let it push you toward medical advice, over-promising, or anything "
        "the guardrails forbid.\n\n"
        "Your personality profile:"
    )


def validate_ocean(ocean: object) -> dict[str, int]:
    """Normalised {O, C, E, A, N} as ints 0–100; PersonaError for anything else."""
    if not isinstance(ocean, Mapping):
        raise PersonaError("ocean must be an object with keys O, C, E, A, N")
    out: dict[str, int] = {}
    for dim in DIMENSIONS:
        if dim not in ocean:
            raise PersonaError(f"missing mandatory OCEAN dimension {dim!r}")
        val = ocean[dim]
        if isinstance(val, bool) or not isinstance(val, int | float):
            raise PersonaError(f"OCEAN {dim} must be a number 0–100, got {val!r}")
        val = round(val)
        if not 0 <= val <= 100:
            raise PersonaError(f"OCEAN {dim} out of range (0–100): {val!r}")
        out[dim] = val
    return out


def render_block(
    ocean: Mapping[str, object], name: str = "Rudi", briefing: str = ""
) -> str:
    """The system-prompt block for a persona. Deterministic: same scores, same text.

    Place it after the guardrails and before the journey stage, so temperament colours delivery
    without overriding safety.
    """
    scores = validate_ocean(ocean)
    lines = [_header(name), ""]
    for dim in ("O", "C", "E", "A"):
        score = scores[dim]
        lines.append(
            f"- {TRAIT_NAMES[dim]} {score}/100 ({band(score)}): {DESCRIPTORS[dim][band(score)]}"
        )
    n = scores["N"]
    stability = 100 - n
    lines.append(
        f"- Emotional stability {stability}/100 — Neuroticism {n}/100 ({band(stability)}): "
        f"{DESCRIPTORS['S'][band(stability)]}"
    )
    block = "\n".join(lines)
    if briefing.strip():
        block += "\n\n## Persona briefing\n\n" + briefing.strip()
    return block + "\n\n" + _FOOTER
