"""language-mirroring-and-i18n: reply in the person's language and report it, so the
conversation's last-used language is remembered (and the clone's own strings follow it)."""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.i18n import normalize_locale
from rudi_platform.skills.contract import SkillRequest, SkillResult


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        return SkillResult(
            output={"prompt": [self.part("language", self.section(req, "note"))]}
        )

    def on_signals(self, req: SkillRequest) -> SkillResult:
        code = normalize_locale((req.input.get("signals") or {}).get("lang"))
        return SkillResult(output={"locale": code}) if code else SkillResult()
