"""
MEET_RUDI Training Arena — the TESTER: the LLM that plays the simulated user in intent and hybrid
cases.

The system prompt is a versioned template (prompts/arena_tester_prompt.md in the arena bucket)
filled with the persona, the objective and where the conversation stands. The conversation is
given from the persona's side: Rudi's messages arrive as `user`, the persona's own as
`assistant`, so the model's next `assistant` message is the persona speaking.

The tester answers {"message", "pushing", "note"}. `pushing: true` marks an attempt — a message
that actively pursues the objective — which is what `max_attempts` counts.
"""

import string

import arena_llm

LANGUAGE = {"en": "English", "nl": "Dutch (Flemish, as spoken in Belgium)"}
MAX_MESSAGE = 1500


class TesterError(Exception):
    """An unusable answer. `result` is the call that produced it — it was paid for."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def system_prompt(template, spec, turn, max_turns, attempts):
    p = spec.get("persona") or {}
    style = p.get("style") or {}
    intent = spec.get("intent") or {}
    stop = spec.get("stop") or {}
    values = {
        "name": p.get("display_name") or "the user",
        "language": LANGUAGE.get(spec.get("locale"), "English"),
        "age_band": p.get("age_band") or "not specified",
        "background": (p.get("background") or "not specified").strip(),
        "attitude": (p.get("attitude") or "not specified").strip(),
        "verbosity": style.get("verbosity") or "short",
        "tone": style.get("tone") or "natural",
        "typos": style.get("typos") or "none",
        "emoji": style.get("emoji") or "none",
        "objective": intent.get("objective") or "",
        "reveal": "\n".join("- " + r for r in intent.get("reveal") or []) or "- (nothing in particular)",
        "turn": turn,
        "max_turns": max_turns,
        "attempts": attempts,
        "max_attempts": stop.get("max_attempts") or "no limit",
    }
    return string.Template(template).safe_substitute(values)


def messages(template, spec, transcript, turn, max_turns, attempts):
    out = [{"role": "system", "content": system_prompt(template, spec, turn, max_turns, attempts)}]
    for e in transcript:
        out.append({"role": "assistant" if e.get("from") == "user" else "user", "content": e.get("text") or ""})
    if out[-1]["role"] == "assistant":
        # A system-led opening (no Rudi message yet to answer) still needs a turn to respond to.
        out.append({"role": "user", "content": "(continue the conversation)"})
    return out


def next_message(cascade, template, spec, transcript, turn, max_turns, attempts, temperature=0.7, max_tokens=400):
    """(message dict, arena_llm.Result). Raises TesterError on an unusable answer, and the
    cascade's own errors (Busy, CascadeFailed) unchanged."""
    res = cascade.chat(messages(template, spec, transcript, turn, max_turns, attempts),
                       json_mode=True, max_tokens=max_tokens, temperature=temperature)
    obj = arena_llm.parse_json_reply(res.text)
    text = obj.get("message")
    if not isinstance(text, str) or not text.strip():
        raise TesterError("the tester answered without a message", res)
    return {"text": text.strip()[:MAX_MESSAGE], "pushing": bool(obj.get("pushing")),
            "note": str(obj.get("note") or "")[:200]}, res
