"""
MEET_RUDI — Twilio ConversationRelay protocol.

Everything that knows the wire format lives here, so the handler in ws.py can be about the
conversation rather than about Twilio.

Twilio sends us JSON *text*, never audio:

    setup      session and call identity, plus the customParameters we put in the TwiML
    prompt     one finished utterance from the patient, already transcribed
    interrupt  the patient talked over Rudi; carries what he had said so far
    dtmf       a keypress
    error      a message we sent was rejected

We reply with text tokens Twilio speaks, or `end` to hang up. Because Twilio owns speech in
both directions here, the endpointing and barge-in handling the bench had to build by hand
come for free — see VOICE_ATTRS.
"""

import json

# TwiML attributes for <ConversationRelay>. These are the dials the bench spent real effort
# reproducing in the browser; here they are configuration.
#
#   speechTimeout   how long a silence must run before the turn is considered finished.
#                   The bench's silenceMs, in Twilio's hands. 600-5000ms.
#   interruptible   barge-in. "any" lets the patient cut Rudi off mid-sentence.
#   reportInputDuringAgentSpeech
#                   deliver what they said while Rudi was still talking, rather than dropping it.
#   hints           transcription hints — the patient's name and topic, exactly as the bench
#                   feeds them to Whisper, because those are the words ASR most often mangles.
DEFAULT_VOICE_ATTRS = {
    "ttsProvider": "Google",
    "voice": "en-US-Journey-D",
    "language": "en-US",
    "transcriptionProvider": "Deepgram",
    "speechTimeout": "1200",
    "interruptible": "any",
    "reportInputDuringAgentSpeech": "speech",
    "ignoreBackchannel": "true",
    "eotThreshold": "0.75",
}


def _attr(value):
    """Escape for an XML attribute. Topics are free text and will contain quotes eventually."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def build_twiml(ws_url, call_id, attrs=None, hints=""):
    """TwiML returned when the patient answers.

    `call_id` rides through as a customParameter and comes back in the setup message. It is the
    only thread linking this socket to the patient's record — without it the handler would know
    a call had connected but not whose.

    Note there is no welcomeGreeting: Rudi's opening is generated, not canned, because it has to
    name the person, summarise their topic and carry the AI disclosure.
    """
    merged = dict(DEFAULT_VOICE_ATTRS)
    merged.update(attrs or {})
    if hints:
        merged["hints"] = hints[:1000]

    rendered = " ".join('%s="%s"' % (k, _attr(v)) for k, v in sorted(merged.items()))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Connect>'
        '<ConversationRelay url="%s" %s>'
        '<Parameter name="call_id" value="%s"/>'
        '</ConversationRelay>'
        '</Connect></Response>'
    ) % (_attr(ws_url), rendered, _attr(call_id))


def say(text, last=True, interruptible=True):
    """A text token for Twilio to speak.

    interruptible=True by default and deliberately: a patient who wants to cut in mid-sentence
    is telling us something, and making Rudi un-interruptible was the single worst thing about
    the bench before it was fixed.
    """
    return json.dumps({"type": "text", "token": text, "last": bool(last),
                       "interruptible": bool(interruptible), "preemptible": False})


def hang_up(reason):
    return json.dumps({"type": "end",
                       "handoffData": json.dumps({"reasonCode": reason})})


def parse(raw):
    """Twilio's frame -> dict. Never raises: an unparseable frame must not kill a live call."""
    try:
        message = json.loads(raw)
        return message if isinstance(message, dict) else {"type": "unknown"}
    except (ValueError, TypeError):
        return {"type": "unknown"}


# --------------------------------------------------------------------------- voicemail

# Twilio's Answering Machine Detection does not cover Belgium (US and Canada only), so an
# outbound call that reaches a voicemail box looks exactly like one that reached a person.
# This is the fallback: voicemail greetings are long, uninterrupted, and say recognisable
# things. A human answering a call says "hello".
#
# Deliberately conservative. Hanging up on a real patient is far worse than talking to an
# answerphone for six seconds, so every rule below needs the utterance to be long AS WELL AS
# matching — no short greeting can ever trip it.
VOICEMAIL_PHRASES = (
    "leave a message", "leave your message", "after the tone", "after the beep",
    "not available", "unable to take your call", "can't take your call",
    "cannot take your call", "voicemail", "voice mail", "record your message",
    "at the sound of the", "please leave",
    # Dutch/Flemish, for when the pilot moves off English
    "laat een bericht", "spreek een bericht", "na de toon", "niet beschikbaar",
    "voicemail van", "is niet bereikbaar",
)
VOICEMAIL_MIN_CHARS = 60


def looks_like_voicemail(text):
    """True when the FIRST thing we hear looks like an answerphone greeting."""
    lowered = (text or "").lower()
    if len(lowered) < VOICEMAIL_MIN_CHARS:
        return False
    return any(phrase in lowered for phrase in VOICEMAIL_PHRASES)
