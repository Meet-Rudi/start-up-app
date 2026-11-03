"""
MEET_RUDI platform — skill packs that exist before any manifest does.

The Baseline pack (decision #27) is included in every new clone. Removing or changing one of its
skills is allowed only with a contract addendum, our review and an arena gate (G5); the composer
reports it as a review reason. The slugs reuse the catalogue ids of today's guard skills, plus
`acceptable-use` for the exclusions; their manifests arrive with the Phase 1 engine.
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
