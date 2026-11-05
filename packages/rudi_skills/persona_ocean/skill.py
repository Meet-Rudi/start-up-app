"""persona-ocean: the persona's temperament block, after the safety rules (catalogue
persona-ocean). The persona itself (name, OCEAN scores, briefing) is clone configuration."""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.persona.ocean import render_block
from rudi_platform.skills.contract import SkillRequest, SkillResult


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        p = req.input.get("persona") or {}
        if not p.get("ocean"):
            return SkillResult(output={"prompt": []})
        block = render_block(
            p["ocean"], name=p.get("name") or "", briefing=p.get("briefing") or ""
        )
        return SkillResult(output={"prompt": [self.part("persona", block)]})
