"""
MEET_RUDI — CM, tenant #1: the diabetes lifestyle coach as a clone (blueprint §4.3, decision #24).

Clone version 1 is **today's CM, unchanged**, on WhatsApp and on calls: the same prompt text in
the same order, the same journey and limits, the same personas and documents. Two parity tests
hold it to the live code turn by turn: test_engine_parity.py (WhatsApp:
services/whatsapp/src/responder.py) and test_voice_parity.py (calls: services/call/src/brain.py).
Anything that would change CM's behaviour belongs in a later clone version, after an arena run
(CLAUDE.md §8).

On calls, CM v1 reproduces today's call brain, including three of its quirks, each a visible
Voice-pack setting below (`CALL_QUIRKS`) to remove in a later version after an arena run:
  - the speaking-style block follows the learn prompt but precedes every other stage's prompt;
  - the learn stage gets no time-budget note;
  - **the learn stage never gets the AI-disclosure instruction**, although the call then counts
    it as given. Every first call (GET_TO_KNOW) starts in learn. Flagged as a compliance gap.
Calls carry no persona block and run no objection gate (somebody asking "who is this?" after
picking up is normal on a call), so those two skills are limited to WhatsApp.

What v1 deliberately leaves out, and why:
  - `acceptable-use` (Baseline): CM's prompt has no such block today. The composer reports the
    removal as a review reason; adding it is clone version 2, after an arena batch.
  - Background skills (keep-warm reach-out, profile summary): they come with the scheduler
    (Phase 2). CM's processor and runners keep doing that work.
  - The objection gate's model classifier: a turn skill, with the turn-skill runtime. The word
    list runs in the engine; CM's processor still runs both layers on WhatsApp.

Until PlanetScale exists the registry is in memory, so `seed()` builds CM into a store at start.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from rudi_platform.ids import Cat, new_id
from rudi_platform.registry import (
    PLATFORM,
    Conflict,
    RegistryStore,
    TenantContext,
    approve_live,
    publish_to_staging,
)
from rudi_platform.skills.manifest import SkillManifest
from rudi_platform.skills.packs import (
    BASELINE_SKILLS,
    COACHING_SKILLS,
    CONVERSATION_SKILLS,
    VOICE_CALL_SKILLS,
)

TENANT_SLUG = "cm"
TENANT_NAME = "CM"
CLONE_SLUG = "rudi"
CLONE_NAME = "Rudi"
PURPOSE = (
    "Lifestyle coaching that helps people living with type 2 diabetes keep up the movement, "
    "diet and daily habits of their care plan, through regular conversation on WhatsApp."
)
LOCALES = ["en", "nl", "fr", "de"]
LEFT_OUT = frozenset({"acceptable-use"})  # see the module docstring

# What CM sets differently from the skills' generic defaults (all of it is today's CM text).
OVERRIDES: dict[str, dict] = {
    "crisis-signposting": {"helpline_url": "https://findahelpline.com/countries/be"},
    "learn-about-rudi": {"companion": "health-coaching companion"},
    "goal-setting": {
        "goal_domains": [
            "diabetes",
            "fitness",
            "diet",
            "sleep",
            "stress",
            "habit",
            "other",
        ]
    },
    "commitment-securing": {
        "guidance_domains": ["diabetes", "fitness", "diet", "sleep", "stress", "habit"]
    },
    "call-closing-and-time-budget": {"farewell_in": ["commit"]},
}

# Today's call brain, reproduced exactly (see the module docstring). Remove in a later version.
CALL_QUIRKS: dict[str, dict] = {
    "call-turn-taking-and-speech": {"after_stage_in": ["learn"]},
    "call-closing-and-time-budget": {"no_budget_note_in": ["learn"]},
    "call-opening-and-greeting": {"no_disclosure_in": ["learn"]},
}

# Skills that run on WhatsApp only in CM (the live call brain has neither).
WHATSAPP_ONLY = frozenset({"persona-ocean", "objection-gate"})


def _skills() -> list[dict]:
    out = []
    packs = (
        *BASELINE_SKILLS,
        *CONVERSATION_SKILLS,
        *COACHING_SKILLS,
        *VOICE_CALL_SKILLS,
    )
    for slug in packs:
        if slug in LEFT_OUT:
            continue
        item: dict = {"skill": slug, "major": 1}
        settings = {**OVERRIDES.get(slug, {}), **CALL_QUIRKS.get(slug, {})}
        if settings:
            item["settings"] = settings
        if slug in WHATSAPP_ONLY:
            item["channels"] = ["whatsapp"]
        out.append(item)
    return out


COMPOSITION = {
    "channels": ["whatsapp", "voice"],
    "locales": LOCALES,
    "skills": _skills(),
}

JOURNEY = {
    "format": "rudi-journey@1",
    "start": "learn",
    "greet": {"first": "intro", "again": "welcome_back"},
    "stages": {
        "learn": {
            "skill": "learn-about-rudi",
            "on": {"wants_to_try": {"to": "goal", "clear_history": True}},
        },
        "goal": {
            "skill": "goal-setting",
            "on": {"accepted": "commit", "gave_up": "concluded"},
        },
        "commit": {
            "skill": "commitment-securing",
            "on": {"committed": "committed", "gave_up": "concluded"},
        },
        "committed": {"skill": "check-in-follow-up", "on": {"goal_changed": "commit"}},
    },
    # A call starts where its call goal says (start_phase), else at goal, and ends the moment
    # the person commits.
    "channels": {
        "voice": {"start": "goal", "on": {"commit": {"committed": "concluded"}}},
    },
}

# Keys in CM's legacy data bucket (meetrudi-ai-data-<account>), as the live services read them.
# Today WhatsApp coaches from diabetes-t2d-guidance.md (HEALTH_GUIDANCE_KEY) while calls prefer
# the newer health-coaching-guidance.md; v1 keeps that, a later version should pick one.
ASSETS = {
    "about": "contexts/rudi-context.md",
    "health_guidance": "contexts/diabetes-t2d-guidance.md",
    "health_guidance@voice": "contexts/health-coaching-guidance.md",
}

# services/whatsapp/seed/personalities; seed-rudi-v2 is DEFAULT_PERSONALITY_SLUG in production.
PERSONAS = {
    "seed-rudi-v2": {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19},
    "seed-rudi-v3": {"O": 69, "C": 81, "E": 64, "A": 70, "N": 19},
}
DEFAULT_PERSONA = "seed-rudi-v2"
SETTINGS = {"assets": ASSETS, "default_persona": DEFAULT_PERSONA}

PARITY_EVIDENCE = "packages/rudi_platform/tests/test_engine_parity.py"


@dataclass(frozen=True)
class SeededClone:
    tenant_id: str
    clone_id: str
    version_id: str
    review_reasons: tuple[str, ...]


def release_skills(store: RegistryStore, manifests: Iterable[SkillManifest]) -> None:
    """Register and release the skill versions of this build.

    Their release gate is parity with the live engine, not an arena gate: these versions are the
    live engine, cut into skills. The audit log says so for every release.
    """
    for manifest in manifests:
        try:
            record = store.register_skill_version(PLATFORM, manifest)
        except Conflict:
            continue  # already in the inventory
        gate = new_id(Cat.GATE_RUN)
        store.release_skill_version(PLATFORM, record.id, gate)
        store.append_audit(
            PLATFORM,
            "skill.release",
            target_id=record.id,
            decision={
                "gate_run": gate,
                "gate": "legacy-parity",
                "evidence": PARITY_EVIDENCE,
            },
        )


def seed(
    store: RegistryStore,
    manifests: Iterable[SkillManifest],
    cell_id: str | None = None,
) -> SeededClone:
    """CM's tenant, clone and live version 1 in `store`."""
    if cell_id is None:
        cell_id = store.create_cell(PLATFORM, "pool", "pooled").id
    release_skills(store, manifests)
    tenant = store.create_tenant(PLATFORM, TENANT_SLUG, TENANT_NAME, cell_id)
    ctx = TenantContext(tenant.id)
    clone = store.create_clone(ctx, tenant.id, CLONE_SLUG, CLONE_NAME, PURPOSE)
    version = store.create_draft(ctx, clone.id)
    store.update_draft(
        ctx, version.id, composition=COMPOSITION, journey=JOURNEY, settings=SETTINGS
    )
    for key, ocean in PERSONAS.items():
        store.put_persona(ctx, version.id, key, "Rudi", ocean, languages=LOCALES)
    publish_to_staging(store, ctx, version.id)
    live = approve_live(store, PLATFORM, version.id)
    return SeededClone(tenant.id, clone.id, version.id, live.composed.review_reasons)
