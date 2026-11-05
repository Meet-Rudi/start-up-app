"""Shared base for skills whose only behaviour is fixed prompt text."""

from __future__ import annotations

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import SkillRequest, SkillResult


class StaticSkill(RealTimeSkill):
    """Contributes prompt sections as they are: PARTS = ((slot, section, order), ...)."""

    PARTS: tuple[tuple[str, str, int], ...] = ()

    def contribute(self, req: SkillRequest) -> SkillResult:
        return SkillResult(
            output={
                "prompt": [
                    self.part(slot, self.section(req, section), order)
                    for slot, section, order in self.PARTS
                ]
            }
        )
