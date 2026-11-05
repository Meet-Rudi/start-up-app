"""commitment-securing: journey stage "commit" — one concrete, doable next action toward the
goal within `max_attempts` messages.

Port of the commit branch of responder._advance and its runtime note. For a goal whose domain is
in `guidance_domains`, the clone's `health_guidance` asset is added (optional: a clone without one
coaches without it, as CM does when the document is missing).

  commitment_made → what they agreed to, and when → `committed`
  otherwise       → one attempt spent; at the limit → `gave_up`
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import MemoryWrite, SkillRequest, SkillResult

NOTE = (
    '[Runtime: the user\'s goal is: "{goal}". Messages left to secure a commitment = {left}. '
    "If that number is 1, this is your FINAL message — do NOT ask again; give the "
    "short closing (restate the action you suggest and say you'll check in later).]"
)
GUIDANCE_HEADING = "# Health & wellness coaching guidance (lifestyle support only — never medical advice)"


def commitment_text(signals: dict, said: str, memory: dict) -> str:
    """What they just agreed to, in one line: the check-in topic the model reported, else their
    own words, else what was agreed before."""
    return (
        str(signals.get("check_in_about") or "").strip()
        or (said or "").strip()[:200]
        or memory.get("commitment")
        or ""
    )


def _attempts(memory: dict) -> int:
    value = memory.get("commit_attempts")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class Skill(RealTimeSkill):
    def on_enter(self, req: SkillRequest) -> SkillResult:
        return SkillResult(memory_writes=(MemoryWrite("commit_attempts", 0),))

    def contribute(self, req: SkillRequest) -> SkillResult:
        memory = req.input.get("memory") or {}
        body = self.section(req, "body")
        domain = str(memory.get("goal_domain") or "").lower()
        guidance = (req.input.get("assets") or {}).get("health_guidance", "")
        if domain in (req.settings.get("guidance_domains") or ()) and guidance:
            body = f"{body}\n\n{GUIDANCE_HEADING}\n\n{guidance}"
        left = max(1, req.settings["max_attempts"] - _attempts(memory))
        note = NOTE.format(goal=memory.get("goal") or "(their goal)", left=left)
        return SkillResult(
            output={"prompt": [self.part("stage", body), self.part("stage_note", note)]}
        )

    def on_signals(self, req: SkillRequest) -> SkillResult:
        signals = req.input.get("signals") or {}
        memory = req.input.get("memory") or {}
        if signals.get("commitment_made") is True:
            return SkillResult(
                output={"outcome": "committed"},
                memory_writes=(
                    MemoryWrite(
                        "commitment",
                        commitment_text(
                            signals, str(req.input.get("text") or ""), memory
                        ),
                    ),
                    MemoryWrite("commitment_agreed_at", req.input.get("now")),
                ),
            )
        attempts = _attempts(memory) + 1
        return SkillResult(
            output={"outcome": "gave_up"}
            if attempts >= req.settings["max_attempts"]
            else {},
            memory_writes=(MemoryWrite("commit_attempts", attempts),),
        )
