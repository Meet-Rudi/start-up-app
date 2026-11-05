"""scope-and-tone-boundaries: no pressure, no promised results, stay on the goal, short replies.

Its four rules sit between other skills' rules in the rules block (the `order` values), exactly
where they stand in CM's guardrails today.
"""

from rudi_skills._base import StaticSkill


class Skill(StaticSkill):
    PARTS = (
        ("rules", "no_pressure", 40),
        ("rules", "no_promises", 50),
        ("rules", "stay_on_goal", 70),
        ("rules", "short_replies", 80),
    )
