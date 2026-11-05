"""
MEET_RUDI platform — skill packs: named bundles a clone starts from (blueprint §4.3).

  Baseline      included in every new clone (decision #27). Removing or changing one of its skills
                is allowed only with a contract addendum, our review and an arena gate (G5); the
                composer reports it as a review reason.
  Conversation  the basics every text clone uses: temperament, language, time, scope and tone.
  Coaching      today's CM engine cut into skills: the journey stages learn → goal → commit →
                check-in loop, the check-in promise, and the coaching rules.
  Voice call    today's call brain cut into skills: the opening and AI disclosure, speech style,
                the call brief with its goals and WhatsApp hand-off, closing and time budget.
                Voicemail screening and dial rules stay in the voice adapter.

Real-time skills only for now. The Coaching pack's background skills (keep-warm reach-out,
profile summary) join it with the scheduler (Phase 2).
Slugs keep today's catalogue ids (services/training-arena/seed/skills/rudi-skills.yaml), so arena
findings that cite a skill id still point at the right skill.
"""

from __future__ import annotations

BASELINE_SKILLS: tuple[str, ...] = (
    "ai-identity-disclosure",
    "crisis-signposting",
    "confidentiality-injection-resistance",
    "prompt-leak-guard",
    "objection-gate",
    "acceptable-use",
)

CONVERSATION_SKILLS: tuple[str, ...] = (
    "persona-ocean",
    "language-mirroring-and-i18n",
    "time-awareness",
    "scope-and-tone-boundaries",
)

COACHING_SKILLS: tuple[str, ...] = (
    "medical-advice-refusal",
    "goal-scope-rejection",
    "learn-about-rudi",
    "goal-setting",
    "commitment-securing",
    "check-in-follow-up",
    "check-in-promise",
)

VOICE_CALL_SKILLS: tuple[str, ...] = (
    "call-opening-and-greeting",
    "call-turn-taking-and-speech",
    "call-goals-and-whatsapp-handoff",
    "call-closing-and-time-budget",
)

PACKS: dict[str, tuple[str, ...]] = {
    "baseline": BASELINE_SKILLS,
    "conversation": CONVERSATION_SKILLS,
    "coaching": COACHING_SKILLS,
    "voice-call": VOICE_CALL_SKILLS,
}
