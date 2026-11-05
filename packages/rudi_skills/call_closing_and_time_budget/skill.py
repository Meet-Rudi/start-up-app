"""call-closing-and-time-budget: ending a call well (Voice call pack).

Port of services/call/src/brain.py `FAREWELL`, `_time_note` and the out-of-time rule in `turn`:

  - in a stage listed in `farewell_in` (CM: commit), the model is told that the turn on which
    they commit is also goodbye, because the call ends there;
  - as the call's time runs out (`max_minutes` from the call, else `default_max_minutes`), the
    model is told to steer to a close, then that this is its final message;
  - once less than a minute is left, the session concludes after this turn, whatever was said.

`no_budget_note_in` lists stages without the time note: today's live brain leaves it out of the
learn stage, and CM v1 reproduces that. Leave it empty for every new clone.
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import SkillRequest, SkillResult

OUT_OF_TIME = (
    "[Runtime: the call is out of time. This is your FINAL message. Do not ask "
    "another question. Confirm what they have agreed to, say when you will be back "
    "in touch, thank them by name and wish them well before saying goodbye. Up to "
    "four short sentences.]"
)
WRAP_UP = (
    "[Runtime: about {minutes} minutes of call time remain. Begin steering toward a close: "
    "get one concrete commitment and wrap up.]"
)


class Skill(RealTimeSkill):
    def _clock(self, req: SkillRequest) -> tuple[int, int]:
        """(seconds left, budget in seconds)."""
        context = req.context or {}
        call = context.get("call") or {}
        try:
            minutes = int(
                call.get("max_minutes") or req.settings["default_max_minutes"]
            )
        except (TypeError, ValueError):
            minutes = req.settings["default_max_minutes"]
        budget = max(1, minutes) * 60
        try:
            elapsed = int(context.get("elapsed_s") or 0)
        except (TypeError, ValueError):
            elapsed = 0
        return budget - elapsed, budget

    def contribute(self, req: SkillRequest) -> SkillResult:
        stage = req.input.get("stage")
        parts = []
        if stage in req.settings["farewell_in"]:
            parts.append(self.part("stage_note", self.section(req, "farewell"), 90))
        if stage not in req.settings["no_budget_note_in"]:
            left, _ = self._clock(req)
            if left <= 60:
                parts.append(self.part("budget", OUT_OF_TIME))
            elif left <= 150:
                parts.append(
                    self.part("budget", WRAP_UP.format(minutes=max(1, left // 60)))
                )
        return SkillResult(output={"prompt": parts})

    def on_signals(self, req: SkillRequest) -> SkillResult:
        left, _ = self._clock(req)
        return SkillResult(output={"conclude": True}) if left <= 60 else SkillResult()
