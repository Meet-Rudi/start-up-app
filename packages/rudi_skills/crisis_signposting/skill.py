"""crisis-signposting: stop coaching and share a helpline on crisis signals (Baseline).

The helpline is a setting: the generic default lets the person pick their country; a clone
serving one country points at it (CM: findahelpline.com/countries/be).
"""

from rudi_skills._base import StaticSkill


class Skill(StaticSkill):
    PARTS = (("rules", "rule", 30),)
