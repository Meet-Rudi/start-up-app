"""goal-setting: journey stage "goal" — one self-directed goal, with at most `max_clarifiers`
clarifying questions and `max_rejects` rejections.

Port of the goal branch of responder._advance and its runtime note:
  accepted            → goal (the model's one-liner, else their words), domain → `accepted`
  rejected            → one more rejection; at the limit → `gave_up`
  unclear / missing   → one more clarifier; with none left, their words become the goal → `accepted`
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import MemoryWrite, SkillRequest, SkillResult

NOTE = (
    "[Runtime: clarifying questions left = {left}. If 0, you MUST now decide accept or "
    "reject — do not ask another question. Reject attempts left = {rejects}.]"
)
FALLBACK_GOAL = "your goal"  # prompt-internal, as today


def _count(memory: dict, name: str) -> int:
    value = memory.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class Skill(RealTimeSkill):
    def values(self, req: SkillRequest) -> dict[str, object]:
        domains = req.settings.get("goal_domains") or []
        return {
            **super().values(req),
            "goal_domains": ", ".join(f'"{d}"' for d in domains),
        }

    def _left(self, req: SkillRequest) -> tuple[int, int]:
        memory = req.input.get("memory") or {}
        clarifiers = max(
            0, req.settings["max_clarifiers"] - _count(memory, "clarifiers_used")
        )
        rejects = max(0, req.settings["max_rejects"] - _count(memory, "reject_count"))
        return clarifiers, rejects

    def on_enter(self, req: SkillRequest) -> SkillResult:
        return SkillResult(
            memory_writes=(
                MemoryWrite("clarifiers_used", 0),
                MemoryWrite("reject_count", 0),
            )
        )

    def contribute(self, req: SkillRequest) -> SkillResult:
        left, rejects = self._left(req)
        return SkillResult(
            output={
                "prompt": [
                    self.part("stage", self.section(req, "body")),
                    self.part("stage_note", NOTE.format(left=left, rejects=rejects)),
                ]
            }
        )

    def on_signals(self, req: SkillRequest) -> SkillResult:
        signals = req.input.get("signals") or {}
        memory = req.input.get("memory") or {}
        said = str(req.input.get("text") or "")
        status = signals.get("goal_status")
        if status == "rejected":
            count = _count(memory, "reject_count") + 1
            gave_up = count >= req.settings["max_rejects"]
            return SkillResult(
                output={"outcome": "gave_up"} if gave_up else {},
                memory_writes=(MemoryWrite("reject_count", count),),
            )
        if status == "accepted":
            return self._accept(
                signals.get("goal") or said or FALLBACK_GOAL,
                signals.get("goal_domain") or "other",
            )
        left, _ = self._left(req)
        if left <= 0:  # the clarifier budget is spent: take what they said as the goal
            return self._accept(said or FALLBACK_GOAL, "other")
        return SkillResult(
            memory_writes=(
                MemoryWrite("clarifiers_used", _count(memory, "clarifiers_used") + 1),
            )
        )

    @staticmethod
    def _accept(goal: object, domain: object) -> SkillResult:
        return SkillResult(
            output={"outcome": "accepted"},
            memory_writes=(
                MemoryWrite("goal", goal),
                MemoryWrite("goal_domain", domain),
            ),
        )
