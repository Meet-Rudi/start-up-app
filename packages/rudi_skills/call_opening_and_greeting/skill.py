"""call-opening-and-greeting: how a call opens, and the AI disclosure (Voice call pack).

Port of services/call/src/brain.py: the opening paragraph of `_call_brief(opening=True)` and
`DISCLOSURE`. The opening is a short greeting that hands the turn straight back (the person says
"hello" the moment they pick up). The disclosure comes on the first turn after they have spoken,
because a disclosure delivered over someone saying "hello?" has been emitted, not understood
(EU AI Act Art. 50); from then on the conversation memory says it was given.

`no_disclosure_in` lists stages where the instruction is not added. It exists because today's
live brain never adds it in the learn stage (the learn branch of build_system skips it) while
still marking it as given; CM v1 reproduces that. Leave it empty for every new clone.
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import MemoryWrite, SkillRequest, SkillResult


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        i = req.input
        if i.get("opening"):
            return SkillResult(
                output={"prompt": [self.part("call", self.section(req, "opening"), 15)]}
            )
        memory = i.get("memory") or {}
        if (
            memory.get("disclosed")
            or i.get("stage") in req.settings["no_disclosure_in"]
        ):
            return SkillResult(output={"prompt": []})
        return SkillResult(
            output={"prompt": [self.part("call", self.section(req, "disclosure"), 20)]}
        )

    def on_signals(self, req: SkillRequest) -> SkillResult:
        if (req.input.get("memory") or {}).get("disclosed"):
            return SkillResult()
        return SkillResult(memory_writes=(MemoryWrite("disclosed", True),))
