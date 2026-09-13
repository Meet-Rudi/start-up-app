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
#                   The bench's silenceMs, in Twilio's hands. Range 600-5000ms.
#
#                   This sits in FRONT of everything else, so it is the largest single term in
#                   the pause a caller actually feels — larger than the model, which measures
#                   300-580ms. It started at 1200ms, carried over from the browser bench where
#                   endpointing too early was unrecoverable. Here it is not: interruptible="any"
#                   means an early reply can simply be talked over. Being slightly too eager is
#                   recoverable; being slow is always slow. Tuned down by ear on live
#                   calls: 1200 -> 800 -> 700ms.
#   eotThreshold    how confident Twilio must be that the turn ended. Lower commits sooner.
#   interruptible   barge-in. "any" lets the patient cut Rudi off mid-sentence.
#   reportInputDuringAgentSpeech
#                   deliver what they said while Rudi was still talking, rather than dropping it.
#   hints           transcription hints — the patient's name and topic, exactly as the bench
#                   feeds them to Whisper, because those are the words ASR most often mangles.
DEFAULT_VOICE_ATTRS = {
    "transcriptionProvider": "Deepgram",
    "speechTimeout": "700",
    "interruptible": "any",
    "reportInputDuringAgentSpeech": "speech",
    "ignoreBackchannel": "true",
    "eotThreshold": "0.6",
}

# Per-language locale, and an ORDERED CASCADE of voices to try.
#
# The cascade is resolved at DIAL time, not mid-call. That is forced by the protocol: the
# `language` message can switch ttsLanguage and transcriptionLanguage on a live call, but it
# accepts neither `voice` nor `ttsProvider` — those are fixed in the TwiML when the call starts.
# So there is no way to fail over to another voice once someone has answered; the choice has to
# be right before the phone rings. See pick_voice() for how that choice is made.
#
# An entry with no `voice` means "let the provider pick its default for this locale", and an
# empty entry means "let Twilio pick" — each step is strictly more conservative than the last,
# so where a cascade has a tail it is something that cannot itself be misconfigured.
#
# DUTCH IS DELIBERATELY DIFFERENT: it has no tail. Luk Belcer was hand-picked and listened to,
# and every Dutch-speaking locale gets him or nothing — nl, nl-BE and nl-NL alike. A silent
# substitution would put an unvetted voice in front of a patient and, worse, would do it
# invisibly: the pilot would be measuring a voice nobody chose. A dead-air call is a louder
# failure than a wrong-voice call, and a louder failure is the one we want here. See pick_voice.
#
# nl-NL keeps its own locale — that drives Deepgram's transcription and the TTS language hint,
# where Netherlands-Dutch is genuinely the better match — while the voice stays Flemish. The
# accent is Luk's either way; the pilot cohort is Belgian and he is the voice we tested.
FLEMISH_VOICE = "ppGIZI01uUlIWI734dUU"     # Luk Belcer — hand-picked, never substituted
_FLEMISH_ONLY = [{"ttsProvider": "ElevenLabs", "voice": FLEMISH_VOICE}]

VOICE_PROFILES = {
    "en": {
        "language": "en-US",
        "cascade": [
            {"ttsProvider": "Google", "voice": "en-US-Journey-D"},
            {},
        ],
    },
    "nl": {"language": "nl-BE", "cascade": list(_FLEMISH_ONLY)},
    "fr": {"language": "fr-BE", "cascade": [{}]},        # Wallonia, not fr-FR
    "de": {"language": "de-DE", "cascade": [{}]},
}
VOICE_PROFILES["nl-be"] = VOICE_PROFILES["nl"]
VOICE_PROFILES["nl-nl"] = {"language": "nl-NL", "cascade": list(_FLEMISH_ONLY)}


def profile_for(language):
    key = str(language or "en").strip().lower()
    return (VOICE_PROFILES.get(key)
            or VOICE_PROFILES.get(key.split("-")[0])
            or VOICE_PROFILES["en"])


def voice_key(option):
    """Stable id for a cascade step, used as the key in the voice-health record."""
    return "%s:%s" % (option.get("ttsProvider", "twilio"), option.get("voice", "default"))


def pick_voice(language, health=None):
    """First cascade step this language has not been seen to fail on. Returns (option, key).

    There is no Twilio API that validates a ConversationRelay voice ahead of a call, and no way
    to change voice once one is in progress — so "test before dialling" is not literally
    available. This is the closest honest equivalent: a voice that has demonstrably failed is
    skipped on every subsequent call, so a misconfigured voice costs one call rather than all
    of them.

    A single-entry cascade is therefore a PIN, not a preference: every step is exhausted, the
    loop falls through, and the one voice is returned however sick it looks. That is what Dutch
    wants — Luk Belcer or nothing. The health record still gets written either way, so the
    failure stays visible in `calls/_voices/health.json` even though nothing acts on it.
    """
    health = health or {}
    cascade = profile_for(language).get("cascade") or [{}]
    for option in cascade:
        if health.get(voice_key(option), {}).get("ok") is not False:
            return option, voice_key(option)
    return cascade[-1], voice_key(cascade[-1])


def voice_attrs_for(language, overrides=None, health=None):
    """Shared dials + locale + the healthiest cascade step, then any per-call override."""
    profile = profile_for(language)
    option, _key = pick_voice(language, health)
    merged = dict(DEFAULT_VOICE_ATTRS)
    merged["language"] = profile["language"]
    merged.update(option)
    merged.update(overrides or {})
    return merged


def _attr(value):
    """Escape for an XML attribute. Topics are free text and will contain quotes eventually."""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def build_twiml(ws_url, call_id, attrs=None, hints="", language="en", health=None):
    """TwiML returned when the patient answers.

    `call_id` rides through as a customParameter and comes back in the setup message. It is the
    only thread linking this socket to the patient's record — without it the handler would know
    a call had connected but not whose.

    Note there is no welcomeGreeting: Rudi's opening is generated, not canned, because it has to
    name the person, summarise their topic and carry the AI disclosure.
    """
    merged = voice_attrs_for(language, attrs, health)
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


def build_say_twiml(text, language="en"):
    """A call that only speaks one line and hangs up — no socket, no model, no tokens.

    This is the fallback when there isn't AI headroom to hold a real conversation: better to
    deliver the one sentence that matters than to ring someone and abandon them mid-call.

    It deliberately does NOT use the ConversationRelay voice cascade: those are ElevenLabs and
    Google voice ids, which `<Say>` does not accept. Twilio picks its own default voice for the
    locale, so this sounds different from Rudi — acceptable for a one-line reminder, and the
    reason this is a fallback rather than a feature.
    """
    profile = profile_for(language)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Say language="%s">%s</Say></Response>'
    ) % (_attr(profile["language"]), _attr(text))


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
