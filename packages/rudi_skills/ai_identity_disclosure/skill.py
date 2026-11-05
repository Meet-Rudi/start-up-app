"""ai-identity-disclosure: says it is an AI when asked (Baseline). Catalogue: ai-identity-disclosure."""

from rudi_skills._base import StaticSkill


class Skill(StaticSkill):
    PARTS = (("rules", "rule", 60),)
