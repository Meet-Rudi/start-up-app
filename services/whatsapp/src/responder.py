"""
MEET_RUDI — WhatsApp AI responder (the "Real Rudi" experience, ported from meetrudi-rudi-chat).

The try-rudi web page ran the state machine in the browser and kept the Lambda stateless. On
WhatsApp there is no browser, so the machine lives here and its state is persisted per
conversation (ContactMeta.ai_state). Same phases, prompts, counters, and signals as the web:

    learn  → answer questions about Rudi; detect intent-to-try            (signals.want_to_try)
    goal   → elicit ONE self-directed goal (≤2 clarifiers, ≤3 rejects)    (signals.goal_status…)
    commit → secure one concrete commitment (≤7 turns; T2D guidance)      (signals.commitment_made)
    → concluded → the next inbound starts a fresh session (re-greets)

Guardrails (rudi_guardrails.md, no medical advice) are prepended to every generation, exactly as
the web experience does (§0.2 / §6). Prompt assets are read from the shared S3 data bucket
(seeded by the rudi-chat deploy).
"""

import os
import json

import boto3

import gateway
import i18n

s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]

GUARDRAILS_KEY = "prompts/rudi_guardrails.md"
LEARN_KEY = os.environ.get("LEARN_KEY", "prompts/rudi_learn_prompt_wa.md")  # WhatsApp-aware learn
GOAL_KEY = "prompts/rudi_goal_prompt.md"
COMMIT_KEY = "prompts/rudi_commit_prompt.md"
RUDI_CONTEXT_KEY = "contexts/rudi-context.md"
HEALTH_GUIDANCE_KEY = os.environ.get("HEALTH_GUIDANCE_KEY", "contexts/diabetes-t2d-guidance.md")
HEALTH_DOMAINS = {"diabetes", "fitness", "diet", "sleep", "stress", "habit"}

# Front-end-enforced limits (from try-rudi.html), now enforced here.
MAX_CLARIFIERS = 2
MAX_COMMIT = 7
MAX_REJECTS = 3
MAX_INPUT_CHARS = 4000
MAX_HISTORY = 20  # cap the session history sent to the model (cost + latency)

# Instruct the model (in code, not the versioned S3 prompt) to mirror the user's language and
# report it, so we can persist "last used language" and localize our own canned strings.
LANG_NOTE = ("[Language: write your reply in the SAME language the user is using. In the JSON "
             "signals object also include \"lang\": the ISO 639-1 code of that language "
             "(e.g. \"en\", \"de\", \"fr\", \"nl\").]")

# Channel briefing prepended to every turn (belt-and-braces beyond the WA learn prompt) so Rudi
# never drifts into pretending it's a website or promising a different channel.
CHANNEL = ("[Channel: You are Rudi talking with the person on WhatsApp — via text, voice notes, "
           "and photos/videos they share (voice calls later). You are NOT a website chatbot. If "
           "asked how you communicate or stay in touch, say it's here on WhatsApp; never claim "
           "you'll switch to a website/app or that you can't continue here. If they send a photo "
           "or voice note you can't process yet, acknowledge it warmly and say you'll be able to "
           "look/listen properly soon.]")

# Whenever Rudi says he'll come back at a time, that sentence becomes a scheduled reach-out —
# so he has to report it, and he has to only promise what the channel can actually deliver.
# WhatsApp allows a free-form message for 24h after the person's last one; past that Rudi could
# only send a paid template, which is not a check-in. Hence: inside 24h, or don't promise.
CHECKIN_NOTE = (
    "[Checking back: NEVER end your message leaving the next contact up to them — \"just let me "
    "know when you're ready\" or \"I'm here whenever\" is not an ending, because then nobody "
    "comes back and the conversation dies. Every time the exchange reaches a natural pause, say "
    "when YOU will check in, and report it in the JSON signals as \"check_in_minutes\": <whole "
    "minutes from now> and \"check_in_about\": \"<short phrase for what you'll ask about>\". "
    "Choosing the time: if they committed to doing something within the next day, check in "
    "shortly after it. If they committed to nothing, or their plan is further off than a day — "
    "including when they say they want to rest — still keep the thread warm: tell them you'll "
    "check in around this same time tomorrow and report \"check_in_minutes\": 1320. Two hard "
    "limits: never more than 24 hours from now, and never a time between 21:30 and 06:30, which "
    "is their quiet time — if roughly-tomorrow would land in that window, pick an earlier hour "
    "the same day. Never name a time you have not reported in check_in_minutes.]")

_asset_cache: dict = {}


def _get_s3_text(key: str) -> str:
    if key in _asset_cache:
        return _asset_cache[key]
    text = s3.get_object(Bucket=DATA_BUCKET, Key=key)["Body"].read().decode("utf-8")
    _asset_cache[key] = text
    return text


def _get_s3_text_optional(key: str) -> str:
    try:
        return _get_s3_text(key)
    except Exception as e:  # noqa: BLE001 - a missing optional asset must not break a turn
        print("INFO: optional asset %s unavailable (%s)" % (key, e))
        return ""


def _to_whatsapp(text: str) -> str:
    """Light markdown → WhatsApp: **bold** → *bold* (WhatsApp bold is single-asterisk)."""
    return (text or "").replace("**", "*")


def _runtime_note(phase: str, state: dict) -> str:
    if phase == "goal":
        return ("[Runtime: clarifying questions left = %s. If 0, you MUST now decide accept or "
                "reject — do not ask another question. Reject attempts left = %s.]"
                % (state.get("clarifiers_left", 2), state.get("reject_attempts_left", 3)))
    if phase == "commit":
        return ("[Runtime: the user's goal is: \"%s\". Messages left to secure a commitment = %s. "
                "If that number is 1, this is your FINAL message — do NOT ask again; give the "
                "short closing (restate the action you suggest and say you'll check in later).]"
                % (state.get("goal", "(their goal)"), state.get("attempts_left", 7)))
    return ""


def _build_system(phase: str, state: dict, personality_block: str = "") -> str:
    # Personality shapes tone/style only. It sits AFTER the guardrails (which lead and take
    # precedence) and BEFORE the role/body, exactly as its header text promises.
    pblock = ("\n\n" + personality_block) if personality_block else ""
    if phase == "learn":
        return (_get_s3_text(LEARN_KEY) + pblock
                + "\n\n# About me (context)\n\n" + _get_s3_text(RUDI_CONTEXT_KEY))

    guardrails = _get_s3_text(GUARDRAILS_KEY)
    if phase == "committed":
        note = ("[Runtime: their goal is \"%s\". They agreed to: \"%s\". Ask how THAT went before "
                "anything else, then agree the next small step.]"
                % (state.get("goal") or "(not recorded)",
                   state.get("commitment") or "(not recorded)"))
        return guardrails + pblock + "\n\n" + CHECKIN_PROMPT + "\n\n" + note
    if phase == "goal":
        body = _get_s3_text(GOAL_KEY)
    elif phase == "commit":
        body = _get_s3_text(COMMIT_KEY)
        if (state.get("goal_domain") or "").lower() in HEALTH_DOMAINS:
            guidance = _get_s3_text_optional(HEALTH_GUIDANCE_KEY)
            if guidance:
                body += ("\n\n# Health & wellness coaching guidance (lifestyle support only — "
                         "never medical advice)\n\n" + guidance)
    else:
        raise ValueError("Unknown phase: %r" % phase)

    note = _runtime_note(phase, state)
    return guardrails + pblock + "\n\n" + body + ("\n\n" + note if note else "")


def _parse_envelope(text: str) -> dict:
    """Defensively parse the model's {reply, signals} JSON. Degrade to plain text on failure."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        obj = None
        if start != -1 and end > start:
            try:
                obj = json.loads(text[start:end + 1])
            except (ValueError, TypeError):
                obj = None
    if not isinstance(obj, dict):
        return {"reply": text, "signals": {}}
    reply = obj.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = text
    signals = obj.get("signals") if isinstance(obj.get("signals"), dict) else {}
    return {"reply": reply, "signals": signals}


def new_session(prev_session_id: int = 0, goal: str = None, goal_domain: str = None) -> dict:
    """A fresh session, but not a fresh stranger.

    The goal is carried across deliberately. Wiping it meant that after any restart Rudi asked
    "what would you like to work on?" to someone who had told him days ago — and the stored
    profile, which is fed from this state, kept serving whatever goal was last written because a
    None here reads as "don't change it".
    """
    return {"phase": "learn", "session_id": prev_session_id + 1, "history": [],
            "clarifiers_used": 0, "commit_attempts": 0, "reject_count": 0,
            "goal": goal, "goal_domain": goal_domain, "commitment": None}


# The check-in half of the loop. Kept in code rather than in an S3 prompt because those prompts
# are shared with the call brain, voice-bench and rudi-chat — none of which has this phase.
CHECKIN_PROMPT = (
    "You are **Rudi**, following up on something this person has ALREADY agreed to do.\n\n"
    "This is not a fresh start. Do not greet them as if they have just arrived, do not ask what "
    "they would like to work on, and do not invent a new goal — they have one.\n\n"
    "On this turn:\n"
    "1. Ask how the thing they committed to actually went. Name it, so they can tell you heard "
    "them.\n"
    "2. Take the answer as it comes. Done deserves a short, genuine word of credit; not done "
    "deserves curiosity about what got in the way — never disappointment, never a lecture.\n"
    "3. Then agree the NEXT small step toward the same goal. When they accept one, set "
    "`commitment_made` to true.\n"
    "4. Only if THEY say they want to change or drop the goal: set `goal_status` to "
    "\"accepted\" and put their new goal in `goal`.\n\n"
    "Short and warm, the way a friend asks. Reply in the user's language. Never give medical "
    "advice.\n\n"
    "Respond ONLY as a single JSON object in exactly this shape (no text outside the JSON):\n"
    '{"reply": "<message>", "signals": {"commitment_made": false, "goal_status": null, '
    '"goal": null}}'
)


def _commitment_text(signals: dict, last_user: str, state: dict) -> str:
    """What they just agreed to, in one line.

    Prefers `check_in_about`, which CHECKIN_NOTE already asks the model for on every turn — so
    this needs no change to the commit prompt, which is shared with three other services.
    """
    return (str(signals.get("check_in_about") or "").strip()
            or (last_user or "").strip()[:200]
            or state.get("commitment") or "")


def _advance(state: dict, signals: dict, last_user: str, clarifiers_left) -> None:
    """Port of try-rudi.html processSignals(): mutate `state` per phase + signals."""
    phase = state["phase"]
    if phase == "learn":
        if signals.get("want_to_try") is True:   # switch to the Real-Rudi experience
            state["phase"] = "goal"
            state["history"] = []                 # fresh LLM session for goal/commit
            state["clarifiers_used"] = 0
            state["reject_count"] = 0
        return
    if phase == "goal":
        gs = signals.get("goal_status")
        if gs == "rejected":
            state["reject_count"] = state.get("reject_count", 0) + 1
            if state["reject_count"] >= MAX_REJECTS:
                state["phase"] = "concluded"
        elif gs == "accepted":
            state["goal"] = signals.get("goal") or last_user or "your goal"
            state["goal_domain"] = signals.get("goal_domain") or "other"
            state["phase"] = "commit"
            state["commit_attempts"] = 0
        else:  # unclear / missing
            if (clarifiers_left or 0) <= 0:       # safeguard: force-accept after the clarifier budget
                state["goal"] = last_user or "your goal"
                state["goal_domain"] = "other"
                state["phase"] = "commit"
                state["commit_attempts"] = 0
            else:
                state["clarifiers_used"] = state.get("clarifiers_used", 0) + 1
        return
    if phase == "committed":
        # The loop: check-in -> next commitment -> check-in. A changed goal sends them back
        # through goal-setting; anything else keeps the relationship where it is.
        if signals.get("goal_status") == "accepted" and signals.get("goal"):
            state["goal"] = signals.get("goal")
            state["goal_domain"] = signals.get("goal_domain") or state.get("goal_domain") or "other"
            state["phase"] = "commit"
            state["commit_attempts"] = 0
            state["commitment"] = None
        elif signals.get("commitment_made") is True:
            state["commitment"] = _commitment_text(signals, last_user, state)
        return
    if phase == "commit":
        if signals.get("commitment_made") is True:
            # NOT "concluded". Concluding meant the next message they sent was met with the
            # canned welcome-back and a wiped goal — so agreeing to something was the surest way
            # to make Rudi forget it. A commitment starts the next lap instead.
            state["phase"] = "committed"
            state["commitment"] = _commitment_text(signals, last_user, state)
            return
        state["commit_attempts"] = state.get("commit_attempts", 0) + 1
        if state["commit_attempts"] >= MAX_COMMIT:
            state["phase"] = "concluded"


# Proactive keep-warm reach-out (Rudi initiates, no user turn). Guardrails still apply.
REACHOUT = (
    "You are Rudi, proactively reaching out on WhatsApp to gently keep this person engaged with "
    "their goal — like a caring buddy checking in, unprompted. Write ONE short, warm message "
    "(1–2 sentences): if you know their goal or last step, reference it naturally and ask how "
    "it's going; otherwise ask an open, friendly question. Do NOT reintroduce yourself or greet "
    "with your name. Invite a quick reply. Never give medical advice. Plain text only.\n\n"
    "Do NOT propose a brand-new goal or a new activity they never agreed to. You are asking "
    "about what is already theirs. Suggesting fresh homework out of nowhere reads as not having "
    "listened, which is the opposite of checking in."
)


def reach_out(state: dict, locale: str = i18n.DEFAULT_LOCALE,
              goal: str = None, development: str = None, personality_block: str = "",
              commitment: str = None) -> tuple:
    """Generate a proactive, context-aware keep-warm message → (text, new_state, info).

    Uses the person's goal + most-recent-development (from their profile) plus recent history so
    the reach-out continues the relationship rather than restarting it. Raises (gateway errors)
    so the caller can fall back to a canned nudge; on success the message is appended to the
    session history for continuity.
    """
    state = dict(state or {})
    goal = goal or state.get("goal")
    bits = []
    if goal:
        bits.append('their goal: "%s"' % goal)
    if development:
        bits.append('what was last going on: "%s"' % development)
    ctx = ("What you know — " + "; ".join(bits) + ".") if bits else "You don't know their specific goal yet."
    # This reach-out is Rudi keeping a promise, so it must ask about the thing he promised to ask
    # about. A generic "how's it going" after "I'll check in on your bike ride" reads as having
    # forgotten — which is exactly the failure the commitment machinery exists to prevent.
    if commitment:
        ctx += (' You promised to come back to them specifically about: "%s". Ask about THAT, '
                'directly and warmly, as the reason you are messaging now.' % commitment)
    lang = "[Language: write your message in the user's language (code: %s).]" % (locale or "en")
    pblock = ("\n\n" + personality_block) if personality_block else ""
    system = (_get_s3_text(GUARDRAILS_KEY) + pblock + "\n\n" + REACHOUT + "\n\n[Context] " + ctx
              + "\n\n" + CHANNEL + "\n\n" + lang)

    history = list(state.get("history", []))
    msgs = [{"role": "system", "content": system}] + history[-MAX_HISTORY:]
    if not history:
        msgs.append({"role": "user", "content": "(system: time for a gentle check-in)"})

    result = gateway.generate(msgs, json_mode=False)
    text = _to_whatsapp(_parse_envelope(result["text"])["reply"])
    state["history"] = history + [{"role": "assistant", "content": text}]
    return (text, state, {"model": result.get("model")})


SUMMARIZE = ("In ONE short neutral sentence, summarize what this person's last topic, issue, or "
             "progress was in the conversation. Third-person, factual, no advice, no greeting. "
             "Plain text only.")


def summarize(history: list) -> str:
    """One-line 'most recent development' for the profile from a [{role,content}] history.
    Raises on gateway error (caller falls back)."""
    history = list(history or [])
    if not history:
        return ""
    msgs = [{"role": "system", "content": _get_s3_text(GUARDRAILS_KEY) + "\n\n" + SUMMARIZE}] + history[-MAX_HISTORY:]
    result = gateway.generate(msgs, json_mode=False)
    return _parse_envelope(result["text"])["reply"].strip()[:280]


def respond(state: dict, user_text: str, locale: str = i18n.DEFAULT_LOCALE,
            personality_block: str = "") -> tuple:
    """Advance one turn. Returns (reply_text, new_state, info).

    A fresh or concluded conversation is greeted (no model call); otherwise the phase-specific
    system prompt drives one generation and the signals advance the machine. `locale` is the
    contact's last-used language, used to localize the returning-user greeting; `info["lang"]`
    carries the language the model detected this turn (for the caller to persist).
    `personality_block` is the rendered OCEAN persona block (from `personality.resolve_block`),
    prepended to the reasoning prompt to shape tone/style; "" = no persona flavor.
    """
    state = dict(state or {})
    if not state:                                    # brand-new number → introduce Rudi (English default)
        intro = i18n.t("intro", i18n.DEFAULT_LOCALE)
        ns = new_session(0)
        ns["history"] = [{"role": "assistant", "content": intro}]
        return (_to_whatsapp(intro), ns, {"phase": "learn", "greeted": True, "new_contact": True})
    if state.get("phase") in (None, "", "concluded"):  # returning number → fresh session (last-used locale)
        back = i18n.t("welcome_back", locale)
        ns = new_session(state.get("session_id", 0),
                         goal=state.get("goal"), goal_domain=state.get("goal_domain"))
        ns["history"] = [{"role": "assistant", "content": back}]
        return (_to_whatsapp(back), ns, {"phase": "learn", "greeted": True})

    phase = state["phase"]
    history = list(state.get("history", [])) + [{"role": "user", "content": user_text[:MAX_INPUT_CHARS]}]

    note_state: dict = {}
    clarifiers_left = None
    if phase == "goal":
        clarifiers_left = max(0, MAX_CLARIFIERS - state.get("clarifiers_used", 0))
        note_state = {"clarifiers_left": clarifiers_left,
                      "reject_attempts_left": max(0, MAX_REJECTS - state.get("reject_count", 0))}
    elif phase == "commit":
        note_state = {"attempts_left": max(1, MAX_COMMIT - state.get("commit_attempts", 0)),
                      "goal": state.get("goal"), "goal_domain": state.get("goal_domain")}
    elif phase == "committed":
        # _build_system is handed note_state, not the whole state, so the check-in phase has to
        # carry the two things it is entirely about.
        note_state = {"goal": state.get("goal"), "commitment": state.get("commitment"),
                      "goal_domain": state.get("goal_domain")}

    system = (_build_system(phase, note_state, personality_block) + "\n\n" + CHANNEL
              + "\n\n" + LANG_NOTE + "\n\n" + CHECKIN_NOTE)
    result = gateway.generate([{"role": "system", "content": system}] + history[-MAX_HISTORY:],
                              json_mode=True)
    env = _parse_envelope(result["text"])
    reply, signals = env["reply"], env["signals"]

    state["history"] = history + [{"role": "assistant", "content": reply}]
    _advance(state, signals, user_text, clarifiers_left)
    return (_to_whatsapp(reply), state,
            {"phase": state["phase"], "signals": signals, "model": result.get("model"),
             "lang": i18n.normalize_locale(signals.get("lang"))})
