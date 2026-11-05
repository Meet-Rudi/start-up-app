"""check-in-promise: never leave the next contact open (catalogue check-in-promise).

Port of responder.CHECKIN_NOTE and the check_in_at capture at the end of responder.respond: the
model reports minutes-from-now because it cannot know the clock; the moment is stored whole, so a
later turn can say "today at 19:30". The promise is refused beyond 24 hours, the free-form window.
Quiet hours are clone settings (CM: 21:30–06:30).
"""

from rudi_platform import timeline
from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import MemoryWrite, SkillRequest, SkillResult


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        return SkillResult(
            output={"prompt": [self.part("check_in", self.section(req, "note"))]}
        )

    def on_signals(self, req: SkillRequest) -> SkillResult:
        now = timeline.parse(req.input.get("now")) or timeline.now_utc()
        minutes = (req.input.get("signals") or {}).get("check_in_minutes")
        at = timeline.after_minutes(minutes, now)
        return (
            SkillResult(memory_writes=(MemoryWrite("check_in_at", at),))
            if at
            else SkillResult()
        )
