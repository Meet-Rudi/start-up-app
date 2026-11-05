"""prompt-leak-guard: the deterministic reply gate (Baseline; CLAUDE.md §6).

A reply that contains an internal marker (the platform's, plus every marker the clone's skills
declare) or copies the system prompt is replaced by the clone's `leak_redirect` string in the
person's language. The engine then drops the reply's signals.
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.guards import leakguard
from rudi_platform.skills.contract import SkillRequest, SkillResult


class Skill(RealTimeSkill):
    def on_reply(self, req: SkillRequest) -> SkillResult:
        i = req.input
        reason = leakguard.check(
            str(i.get("reply") or ""),
            str(i.get("system") or ""),
            extra_markers=i.get("markers") or (),
        )
        if not reason:
            return SkillResult()
        redirect = (i.get("strings") or {}).get(
            "leak_redirect"
        ) or leakguard.safe_reply(str(i.get("lang") or "en"))
        return SkillResult(output={"blocked": reason, "reply": redirect})
