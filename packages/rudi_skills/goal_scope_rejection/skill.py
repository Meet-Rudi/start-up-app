"""goal-scope-rejection: coach only goals about the person's own life (Coaching). The goal-setting
stage carries the matching "rejected" handling."""

from rudi_skills._base import StaticSkill


class Skill(StaticSkill):
    PARTS = (("rules", "rule", 20),)
