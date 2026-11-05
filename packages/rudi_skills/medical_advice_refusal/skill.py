"""medical-advice-refusal: never medical advice, diagnoses or treatment instructions (Coaching)."""

from rudi_skills._base import StaticSkill


class Skill(StaticSkill):
    PARTS = (("rules", "rule", 10),)
