"""call-goals-and-whatsapp-handoff: the call brief and how each kind of call must end
(Voice call pack).

Port of services/call/src/brain.py `_call_brief`, `_goal_note` and `_capture_checkin`. The call
details come from the voice adapter in `request.context["call"]`, already made model-safe (the
person's name as an alias, topic and notes scrubbed): user_name, topic, notes, language,
call_goal. The call goal is system-set and never shown to the person:

  SET_NEARTERM_GOAL, GOAL_FOLLOWUP  a call-back promised out loud is reported, so a follow-up
                                    call can be placed (check_in_minutes / check_in_about)
  GET_TO_KNOW                       end by moving them to WhatsApp, where coaching lives
"""

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import MemoryWrite, SkillRequest, SkillResult

MAX_CHECK_IN_MINUTES = 24 * 60


def brief(call: dict) -> str:
    """Outbound means we already know who and why — so the brief says so."""
    name = str(call.get("user_name") or "").strip()
    topic = str(call.get("topic") or "").strip()
    notes = str(call.get("notes") or "").strip()
    lang = str(call.get("language") or "en").strip()
    bits = ["# Call brief", "", "This is an outbound call that you placed."]
    bits.append(f"Speak in the language with code: {lang}.")
    if name:
        bits.append(
            f"The person you called is named {name}. Use their first name naturally, "
            "not in every sentence."
        )
    else:
        bits.append("You do not know this person's name. Do not ask for it.")
    if topic:
        bits.append(
            f'The topic they signed up to talk about is: "{topic}". You already know this — '
            "do not ask them what they want to work on. Summarise it back to them in "
            "your own words and check you have understood it correctly."
        )
    if notes:
        bits.append(f"Additional context: {notes}")
    return "\n".join(bits)


class Skill(RealTimeSkill):
    def contribute(self, req: SkillRequest) -> SkillResult:
        call = (req.context or {}).get("call") or {}
        parts = [self.part("call", brief(call), 10)]
        goal = str(call.get("call_goal") or "").strip().upper()
        if goal in ("SET_NEARTERM_GOAL", "GOAL_FOLLOWUP"):
            parts.append(self.part("call", self.section(req, "check_in"), 30))
        elif goal == "GET_TO_KNOW":
            parts.append(self.part("call", self.section(req, "handoff"), 30))
        return SkillResult(output={"prompt": parts})

    def on_signals(self, req: SkillRequest) -> SkillResult:
        signals = req.input.get("signals") or {}
        try:
            minutes = int(signals.get("check_in_minutes"))
        except (TypeError, ValueError):
            return SkillResult()
        if not 0 < minutes <= MAX_CHECK_IN_MINUTES:
            return SkillResult()
        about = str(signals.get("check_in_about") or "").strip()[:200]
        return SkillResult(
            memory_writes=(
                MemoryWrite("check_in_minutes", minutes),
                MemoryWrite("check_in_about", about),
            )
        )
