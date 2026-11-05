"""time-awareness: the person's local clock on every prompt. History time markers are rendered by
the engine itself (they are part of how history reaches the model)."""

from rudi_platform import timeline
from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import SkillRequest, SkillResult


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        now = timeline.parse(req.input.get("now")) or timeline.now_utc()
        note = timeline.now_note(now, str(req.input.get("tz") or ""))
        return SkillResult(output={"prompt": [self.part("time", note)]})
