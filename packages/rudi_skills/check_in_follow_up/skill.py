"""check-in-follow-up: journey stage "committed" — the check-in loop.

Port of responder.CHECKIN_PROMPT, the "committed" runtime note and the committed branch of
responder._advance:
  goal changed (goal_status accepted with a goal) → new goal, commitment cleared → `goal_changed`
  commitment_made                                → the new step and when it was agreed
"""

from rudi_platform import timeline
from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import MemoryWrite, SkillRequest, SkillResult

from rudi_skills.commitment_securing.skill import commitment_text

NOTE = (
    '[Runtime: their goal is "{goal}". They agreed{agreed} to: "{commitment}".{check_in} Ask how '
    "THAT went before anything else — unless the time they planned is still ahead, in which "
    "case encourage them for it instead of asking for an update. Then agree the next "
    "small step.]"
)


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        memory = req.input.get("memory") or {}
        now = timeline.parse(req.input.get("now")) or timeline.now_utc()
        tz = str(req.input.get("tz") or "")
        # Resolved against this turn's clock: the prompt sees "yesterday at 15:00", never a
        # timestamp it would have to work out.
        agreed = timeline.humanize(memory.get("commitment_agreed_at"), now, tz)
        planned = timeline.due(memory.get("check_in_at"), now, tz)
        note = NOTE.format(
            goal=memory.get("goal") or "(not recorded)",
            agreed=f" {agreed}" if agreed else "",
            commitment=memory.get("commitment") or "(not recorded)",
            check_in=f" You planned to check in on it {planned}." if planned else "",
        )
        return SkillResult(
            output={
                "prompt": [
                    self.part("stage", self.section(req, "body")),
                    self.part("stage_note", note),
                ]
            }
        )

    def on_signals(self, req: SkillRequest) -> SkillResult:
        signals = req.input.get("signals") or {}
        memory = req.input.get("memory") or {}
        if signals.get("goal_status") == "accepted" and signals.get("goal"):
            return SkillResult(
                output={"outcome": "goal_changed"},
                memory_writes=(
                    MemoryWrite("goal", signals.get("goal")),
                    MemoryWrite(
                        "goal_domain",
                        signals.get("goal_domain")
                        or memory.get("goal_domain")
                        or "other",
                    ),
                    MemoryWrite("commitment", None),
                    MemoryWrite("commitment_agreed_at", None),
                ),
            )
        if signals.get("commitment_made") is True:
            return SkillResult(
                memory_writes=(
                    MemoryWrite(
                        "commitment",
                        commitment_text(
                            signals, str(req.input.get("text") or ""), memory
                        ),
                    ),
                    MemoryWrite("commitment_agreed_at", req.input.get("now")),
                )
            )
        return SkillResult()
