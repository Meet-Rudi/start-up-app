"""learn-about-rudi: journey stage "learn" — who the clone is, from the clone's About document,
until the person wants to start (outcome `wants_to_try`). Catalogue id kept for traceability;
nothing in it is CM-specific beyond the `companion` setting and the About asset.

Port of responder._build_system("learn"), the learn branch of responder._advance, and the same
for calls in services/call/src/brain.py (its own prompt, `body_voice`: spoken, on a call).
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import SkillRequest, SkillResult

ABOUT_HEADING = "# About me (context)"  # the leak guard cuts this public section from its copy check


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        about = (req.input.get("assets") or {}).get("about", "")
        body = "body_voice" if req.input.get("channel") == "voice" else "body"
        text = f"{self.section(req, body)}\n\n{ABOUT_HEADING}\n\n{about}"
        return SkillResult(output={"prompt": [self.part("stage", text)]})

    def on_signals(self, req: SkillRequest) -> SkillResult:
        if (req.input.get("signals") or {}).get("want_to_try") is True:
            return SkillResult(output={"outcome": "wants_to_try"})
        return SkillResult()
