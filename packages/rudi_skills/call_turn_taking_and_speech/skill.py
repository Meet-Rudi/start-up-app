"""call-turn-taking-and-speech: speaking, not writing (Voice call pack).

Port of services/call/src/brain.py `VOICE_STYLE`. The speech clean-up of the reply (no markup,
aliases kept intact) is the voice channel's delivery style, `engine.channels.speakable`.

The block sits before the stage text. `after_stage_in` lists stages where it follows the stage
text instead: today's live brain places it after the learn prompt and before every other stage's
prompt, and CM v1 reproduces that. Leave it empty for every new clone.
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import SkillRequest, SkillResult


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        text = self.section(req, "style")
        if req.input.get("stage") in req.settings["after_stage_in"]:
            return SkillResult(output={"prompt": [self.part("stage", text, 90)]})
        return SkillResult(output={"prompt": [self.part("style", text)]})
