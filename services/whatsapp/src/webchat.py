"""
MEET_RUDI — one turn of Rudi's web chat, shared by every caller that talks to him in text.

The tester console's chat (Track A) and the Training Arena's `meetrudi-engine-turn` both answer
through `chat_turn()`, so what a tester experiences and what the arena measures cannot drift
apart. The order is the WhatsApp path's, minus the delivery:

    de-identify (deid.scrub_inbound) → responder.respond → restore names (deid.restore_outbound)

Nothing is stored here: the caller owns persistence, and the session semantics of the alias
vault (the tester console keys it to the 24h window; the arena keeps one per case).

`engine` and `personas` default to the real modules. A caller passes its own references so that
its tests, which stub those names in the caller's namespace, keep exercising the same path.
"""

import deid
import personality
import responder

_DETECTOR = deid.HeuristicDetector()


def chat_turn(vault, ai_state, text, locale, persona="", tz="", now=None,
              detector=None, engine=None, personas=None, redacted=""):
    """Run one user message through Rudi. Raises whatever the engine raises (gateway errors).

    Returns a dict:
        masked       the user's message as stored and as the model saw it (identifiers removed,
                     names aliased)
        found        {identifier label: count} removed from the message
        reply        Rudi's reply as the person reads it (names restored)
        reply_masked Rudi's reply as stored
        state        the new conversation state
        info         the responder's info (phase, signals, model, lang, phase_before,
                     leak_blocked, usage)
        vault        the alias vault after this turn, as a dict
    """
    engine = engine or responder
    personas = personas or personality
    masked, found = deid.scrub_inbound(vault, text, detector or _DETECTOR, locale)
    block = personas.resolve_block(persona)
    kwargs = {"locale": locale, "personality_block": block}
    if tz:
        kwargs["tz"] = tz
    if now is not None:
        kwargs["now"] = now
    reply, new_state, info = engine.respond(ai_state, masked, **kwargs)
    return {"masked": masked, "found": found,
            "reply": deid.restore_outbound(vault, reply, redacted=redacted),
            "reply_masked": reply, "state": new_state, "info": info or {},
            "vault": vault.to_dict()}
