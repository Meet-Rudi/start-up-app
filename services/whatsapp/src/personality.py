"""
MEET_RUDI — Rudi personality store (OCEAN psychographics + optional briefing).

A "personality" is *how a given Rudi persona sounds*: five OCEAN scores (0–100, ALL mandatory)
plus an OPTIONAL free-text briefing (.md) for nuance the numbers can't capture. Personalities are
stored in the shared S3 data bucket and rendered into a system-prompt block that shapes tone and
style only — never role, facts, or the safety guardrails (§0/§6). Storage is swappable behind this
module: callers pass a slug and get back a ready-to-prepend prompt block; the S3 layout never
leaks out.

S3 layout (seeded from services/whatsapp/seed/personalities/ by the normal deploy):
    personalities/<slug>/personality.json    # mandatory OCEAN + metadata (schema below)
    personalities/<slug>/<briefing>           # optional .md, named by personality.json.briefing

personality.json — OCEAN keys O,C,E,A,N are mandatory, each an int 0–100:
    {
      "name": "Seed Rudi v2",
      "slug": "seed-rudi-v2",
      "version": 1,
      "ocean": { "O": 59, "C": 71, "E": 57, "A": 60, "N": 19 },
      "briefing": null            # or "briefing.md" (relative to the slug folder)
    }

The OCEAN→text mapping below is deterministic and versioned in code (so it is testable and
rebuildable, like our other prompt assets). N (neuroticism) is rendered as its inverse, Emotional
stability, which reads more naturally as a behavioral instruction.
"""

import os
import json

import boto3

_s3 = boto3.client("s3")
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
PERSONALITIES_PREFIX = os.environ.get("PERSONALITIES_PREFIX", "personalities")
# The persona a conversation gets when the operator hasn't chosen one (meta.persona == "").
DEFAULT_SLUG = os.environ.get("DEFAULT_PERSONALITY_SLUG", "seed-rudi-v2")

DIMENSIONS = ("O", "C", "E", "A", "N")

_cache: dict = {}


class PersonalityError(Exception):
    """A personality could not be loaded or is invalid (missing/out-of-range OCEAN)."""


# --- OCEAN → prompt vocabulary (versioned in code) ---------------------------------------------

def _band(score: int) -> str:
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


# Behavioral descriptor per trait per band. Written as second-person coaching-style instructions
# ("You are…") because they are injected into Rudi's system prompt. Emotional stability is keyed on
# the INVERTED neuroticism score (stability = 100 − N).
_DESCRIPTORS = {
    "O": {  # Openness — curiosity/imagination vs. practicality/routine
        "very low": "You are firmly practical and down-to-earth: stick to proven, concrete advice and conventional framing, and avoid abstract tangents.",
        "low": "You lean practical and grounded, preferring familiar, tried-and-true suggestions over experimental ones and keeping things concrete.",
        "moderate": "You balance fresh ideas with the tried-and-true — curious and happy to explore a new angle or metaphor when it helps, but you keep suggestions grounded and practical rather than novel for its own sake.",
        "high": "You are curious and imaginative, enjoying creative angles, analogies, and fresh perspectives while staying relevant to the person.",
        "very high": "You are highly inventive and exploratory, readily reaching for novel ideas, vivid imagery, and unconventional angles, and delighting in reframing possibilities.",
    },
    "C": {  # Conscientiousness — organized/reliable vs. spontaneous/easygoing
        "very low": "You are loose and spontaneous, going with the flow rather than pushing structure, plans, or follow-through.",
        "low": "You are relaxed about structure, keeping things easygoing and not pressing hard on plans, specifics, or accountability.",
        "moderate": "You are reasonably organized and reliable, keeping the conversation on track and nudging toward next steps without being rigid.",
        "high": "You are organized, dependable, and follow-through-oriented: you keep the conversation purposeful, remember what was agreed, and gently steer toward concrete, specific next steps — precise rather than vague.",
        "very high": "You are meticulous and highly disciplined — precise, methodical, and thorough — holding a clear structure and firmly guiding toward specific, measurable commitments.",
    },
    "E": {  # Extraversion — sociable/energetic vs. reserved/quiet
        "very low": "You are reserved and soft-spoken: keep messages brief and calm, speak only when it adds value, and give the person plenty of space to lead.",
        "low": "You are on the quiet side — gentle and unassuming, listening more than you talk and keeping your energy low-key.",
        "moderate": "You are warm and personable with a friendly, sociable energy, but measured — you don't dominate the chat or over-hype, and you match the person's energy while giving them room.",
        "high": "You are outgoing, expressive, and energetic, bringing lively warmth to the chat, engaging actively, and comfortable leading the conversation with enthusiasm.",
        "very high": "You are highly gregarious and vivacious, radiating enthusiasm and social energy and keeping the conversation animated, upbeat, and full of encouragement.",
    },
    "A": {  # Agreeableness — compassionate/cooperative vs. frank/detached
        "very low": "You are blunt and matter-of-fact, prioritizing candor over comfort — direct, and willing to challenge with little softening.",
        "low": "You are frank and fairly detached: honest and a bit challenging, not overly soft or accommodating.",
        "moderate": "You are kind, empathetic, and cooperative — genuinely on the person's side, supportive and considerate, while still honest enough to offer a caring nudge rather than only telling people what they want to hear.",
        "high": "You are warm, compassionate, and highly supportive: you lead with empathy, validate feelings, cooperate generously, and assume good intent.",
        "very high": "You are deeply caring and accommodating — exceptionally gentle, patient, and affirming, putting the person's feelings and comfort first.",
    },
    # Emotional stability = 100 − N. Bands here are read on the stability value, not on raw N.
    "S": {
        "very low": "You are emotionally expressive and easily affected, feeling things intensely and readily showing worry or concern.",
        "low": "You are somewhat sensitive and easily moved, wearing your emotions openly and showing concern quickly.",
        "moderate": "You are generally even-keeled, staying mostly calm while still showing genuine feeling when it matters.",
        "high": "You are calm and resilient: setbacks and emotional messages don't rattle you, and you stay reassuring and grounded.",
        "very high": "You are remarkably calm, steady, and unflappable — frustration, setbacks, or emotional messages never shake you; you are a stable, safe, reassuring presence and never catastrophize or project anxiety.",
    },
}

_TRAIT_NAMES = {"O": "Openness", "C": "Conscientiousness", "E": "Extraversion", "A": "Agreeableness"}

_HEADER = (
    "# Who you are — personality\n\n"
    "These traits define your temperament and communication style as Rudi. They shape HOW you "
    "speak — tone, energy, warmth, pacing — not WHAT you are allowed to say. Your role, your "
    "factual context, and especially the safety guardrails always take precedence over "
    "personality; never let it push you toward medical advice, over-promising, or anything the "
    "guardrails forbid.\n\n"
    "Your personality profile:"
)

_FOOTER = (
    "Let these traits color your wording and rhythm naturally. Do NOT mention OCEAN, personality, "
    "scores, or that you are configured — simply be this person."
)


# --- validation & loading ----------------------------------------------------------------------

def _validate_ocean(ocean) -> dict:
    if not isinstance(ocean, dict):
        raise PersonalityError("`ocean` must be an object with keys O, C, E, A, N")
    out = {}
    for dim in DIMENSIONS:
        if dim not in ocean:
            raise PersonalityError("missing mandatory OCEAN dimension %r" % dim)
        val = ocean[dim]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise PersonalityError("OCEAN %s must be a number 0–100, got %r" % (dim, val))
        val = int(round(val))
        if not 0 <= val <= 100:
            raise PersonalityError("OCEAN %s out of range (0–100): %r" % (dim, val))
        out[dim] = val
    return out


def load(slug: str) -> dict:
    """Load and validate a personality by slug from S3. Returns a normalized dict:
        {name, slug, version, ocean:{O..N}, briefing_text}
    where briefing_text is "" when no briefing is configured. Raises PersonalityError on a missing
    file, bad JSON, or missing/out-of-range OCEAN (fail fast — a persona with no valid OCEAN is a
    configuration error, not something to silently paper over)."""
    if not slug or "/" in slug or ".." in slug:
        raise PersonalityError("invalid personality slug: %r" % slug)
    if slug in _cache:
        return _cache[slug]

    key = "%s/%s/personality.json" % (PERSONALITIES_PREFIX, slug)
    try:
        raw = _s3.get_object(Bucket=DATA_BUCKET, Key=key)["Body"].read().decode("utf-8")
    except Exception as e:  # noqa: BLE001
        raise PersonalityError("cannot read personality %r (%s): %s" % (slug, key, e))
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise PersonalityError("personality %r is not valid JSON: %s" % (slug, e))

    ocean = _validate_ocean(data.get("ocean"))

    briefing_text = ""
    briefing = data.get("briefing")
    if briefing:
        if "/" in briefing or ".." in briefing:
            raise PersonalityError("invalid briefing filename: %r" % briefing)
        bkey = "%s/%s/%s" % (PERSONALITIES_PREFIX, slug, briefing)
        try:
            briefing_text = _s3.get_object(Bucket=DATA_BUCKET, Key=bkey)["Body"].read().decode("utf-8").strip()
        except Exception as e:  # noqa: BLE001 - a declared-but-missing briefing is a config error
            raise PersonalityError("briefing %r declared but unreadable (%s): %s" % (briefing, bkey, e))

    personality = {
        "name": data.get("name") or slug,
        "slug": data.get("slug") or slug,
        "version": data.get("version", 1),
        "ocean": ocean,
        "briefing_text": briefing_text,
    }
    _cache[slug] = personality
    return personality


# --- rendering ---------------------------------------------------------------------------------

def render_system_block(personality: dict) -> str:
    """Render a personality into the system-prompt block that shapes Rudi's tone/style.

    Deterministic: same OCEAN → same text. Prepend this to the reasoning system prompt (after the
    guardrails, before role/context) so personality colors delivery without overriding safety."""
    ocean = personality["ocean"]
    lines = [_HEADER, ""]
    for dim in ("O", "C", "E", "A"):
        score = ocean[dim]
        lines.append("- %s %d/100 (%s): %s"
                      % (_TRAIT_NAMES[dim], score, _band(score), _DESCRIPTORS[dim][_band(score)]))
    # Neuroticism rendered as Emotional stability (its inverse).
    n = ocean["N"]
    stability = 100 - n
    lines.append("- Emotional stability %d/100 — Neuroticism %d/100 (%s): %s"
                 % (stability, n, _band(stability), _DESCRIPTORS["S"][_band(stability)]))

    block = "\n".join(lines)
    if personality.get("briefing_text"):
        block += "\n\n## Persona briefing\n\n" + personality["briefing_text"]
    block += "\n\n" + _FOOTER
    return block


def load_block(slug: str) -> str:
    """Convenience: load a personality by slug and return its rendered system block."""
    return render_system_block(load(slug))


def resolve_block(slug: str = "") -> str:
    """Slug (or the configured default) → rendered system block, NEVER raising.

    The live send path calls this: a conversation's chosen persona (meta.persona) or, when unset,
    the default. Falls back to the default if the chosen persona is broken, and to an empty string
    if neither resolves — a misconfigured personality must not break a live turn (it just costs the
    persona flavor, not the reply)."""
    tried: list = []
    for candidate in (slug, DEFAULT_SLUG):
        if candidate and candidate not in tried:
            tried.append(candidate)
            try:
                return load_block(candidate)
            except PersonalityError as e:
                print("WARN personality %r unusable (%s)" % (candidate, e))
    return ""


def list_available() -> list:
    """[{slug, name}] for every personality that has a valid personality.json, name-sorted.

    Powers the operator-console dropdown. Personalities that fail validation are skipped (they
    can't be selected until fixed) rather than breaking the whole list."""
    out = []
    token = None
    base = PERSONALITIES_PREFIX.rstrip("/") + "/"
    while True:
        kw = {"Bucket": DATA_BUCKET, "Prefix": base, "Delimiter": "/"}
        if token:
            kw["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kw)
        for cp in resp.get("CommonPrefixes", []):
            slug = cp["Prefix"][len(base):].rstrip("/")
            try:
                p = load(slug)
            except PersonalityError as e:
                print("INFO personality %r skipped in listing (%s)" % (slug, e))
                continue
            out.append({"slug": slug, "name": p["name"]})
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    out.sort(key=lambda x: x["name"].lower())
    return out
