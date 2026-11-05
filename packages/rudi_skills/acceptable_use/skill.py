"""acceptable-use: the acceptable-use exclusions every new clone starts with (Baseline,
decision #27): adults only, no financial, legal or debt advice, never judging people for others."""

from rudi_skills._base import StaticSkill


class Skill(StaticSkill):
    PARTS = (("policies", "block", 20),)
