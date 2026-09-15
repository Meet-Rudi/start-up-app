"""
MEET_RUDI — inbound objection gate.

Rudi can be given a wrong number. One mistyped digit at registration, or a number that has
changed hands, and a person who never contacted us starts receiving proactive health messages
two to three times a week. They have no account, no console and no obvious way to make it stop.
The only place that can possibly notice is the message they send back — so that is where this
looks.

This is the inbound mirror of the outbound guardrail (CLAUDE.md §6): it runs BEFORE the model
sees the turn, and when it fires nothing is generated at all.

Two layers, in this order:

  1. LEXICON — deterministic phrases. Cheap, total, and it cannot fail open because a model was
     rate-limited or an endpoint was down. This is the floor.
  2. CLASSIFIER — one small model call for what a phrase list cannot catch ("ik denk dat je de
     verkeerde persoon hebt"). Advisory ONLY: it can add a detection, never remove one.

Both are deliberately biased towards firing. A false positive costs one frozen thread that an
operator unfreezes in a click; a false negative means we keep messaging somebody who asked us
to stop, which is the failure that ends up in a complaint.

EVERY locale's lexicon is scanned, not just the contact's. That is the whole point: on a wrong
number the locale on file came from a registration that was itself wrong, so it is exactly the
field we cannot trust. Scanning all four costs microseconds.
"""

import re
import json
import unicodedata

# `gateway` is imported lazily inside classify(), NOT here. The deterministic layer is the floor
# this gate stands on, and it must not become unimportable because the model gateway needs an
# environment it happens not to have — an offline eval, a unit test, or a Lambda whose config is
# half-applied. The floor has no dependencies at all.

# Kinds, most actionable first. They all freeze; the distinction is for operator triage.
WRONG_NUMBER = "wrong_number"     # "this isn't me" — the registration is the thing to check
UNSUBSCRIBE = "unsubscribe"       # "stop" — may be a real user who wants out
HOSTILE = "hostile"               # threats, spam accusations, legal — escalate, don't automate

# Phrases matched against the WHOLE message once normalized. Reserved for words that are an
# objection on their own and something else inside a sentence: a health coach is told "ik wil
# stoppen met roken" constantly, and freezing that thread would be a spectacular own goal.
_EXACT = {
    WRONG_NUMBER: ["wie", "wie is dit", "who dis", "qui"],
    UNSUBSCRIBE: [
        "stop", "stopp", "stop stop", "afmelden", "uitschrijven", "stopzetten",
        "unsubscribe", "remove me", "opt out", "optout",
        "desabonner", "desinscription", "arret", "arretez",
        "abmelden", "abbestellen", "aufhoren",
    ],
}

# Phrases matched anywhere in the message. Multi-word, so they carry their own context and do
# not need the whole-message guard above.
_CONTAINS = {
    WRONG_NUMBER: [
        # nl / nl-BE
        "verkeerd nummer", "verkeerde nummer", "verkeerde persoon", "verkeerd persoon",
        "wie is dit", "wie ben jij", "wie bent u", "wie zijt gij",
        "ik ken u niet", "ik ken je niet", "ken ik u niet", "ik ken jou niet",
        "niet de juiste persoon", "je hebt de verkeerde", "u hebt de verkeerde",
        "ik heb me niet ingeschreven", "ik heb me nooit ingeschreven",
        "nooit aangemeld", "nooit ingeschreven", "ik heb hier niet om gevraagd",
        # en
        # Written as they look AFTER normalize(): no apostrophes, no accents.
        "wrong number", "wrong person", "who is this", "who are you",
        "i dont know you", "i do not know you",
        "i didnt sign up", "i never signed up",
        "i never registered", "i didnt register", "i did not ask for this",
        "you have the wrong", "not the right person", "this isnt me", "this is not me",
        # fr
        "mauvais numero", "mauvaise personne", "qui etes vous", "qui est ce",
        "je ne vous connais pas", "je ne me suis pas inscrit", "je nai rien demande",
        # de
        "falsche nummer", "falsche person", "wer ist das", "wer sind sie",
        "ich kenne sie nicht", "ich habe mich nicht angemeldet", "nie angemeldet",
    ],
    UNSUBSCRIBE: [
        "geen berichten meer", "stuur me geen", "stuur mij geen", "niet meer contacteren",
        "stop met mij", "stop met berichten", "haal me van", "schrijf me uit",
        "no more messages", "stop contacting", "stop messaging", "stop texting",
        "dont message me", "do not contact", "take me off",
        "ne me contactez plus", "plus de messages",
        # "arretez de me contacter" is a request, not an insult. It sat under HOSTILE as the
        # broader "arretez de me" and swallowed every polite French opt-out, which would have
        # escalated ordinary unsubscribes to the wrong operator queue.
        "arretez de me contacter", "arretez de menvoyer", "arretez de mecrire",
        "keine nachrichten mehr", "nicht mehr kontaktieren",
    ],
    HOSTILE: [
        "laat me met rust", "hou op", "houd op", "laat mij met rust", "val me niet lastig",
        "dit is spam", "is dit spam", "naar de politie", "mijn advocaat",
        # Dutch separable verbs split across the sentence: "dien ik een klacht IN". Matching
        # "klacht indienen" alone missed the form people actually write. Kept as "een klacht in"
        # rather than bare "klacht", which would catch "ik heb een klacht over mijn rug" — a
        # sentence a health coach should expect to hear.
        "klacht indienen", "een klacht in", "juridische stappen",
        "leave me alone", "stop bothering", "harassment", "harassing",
        "this is spam", "report you", "reporting you", "my lawyer", "the police",
        "laissez moi tranquille", "cest du spam", "porter plainte", "harcelement",
        "lassen sie mich in ruhe", "belastigung", "das ist spam", "anzeige erstatten",
    ],
}

_APOS = re.compile(r"['‘’ʼ`]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize(text):
    """Lowercase, strip diacritics and punctuation, collapse whitespace.

    Diacritics go so that "arrêtez" matches "arretez" and the lexicon can stay plain ASCII —
    people type accents inconsistently on phones, and an accent is never the difference between
    an objection and an ordinary message.

    Apostrophes are DELETED rather than turned into spaces, and the difference matters: with the
    punctuation rule alone "don't" became "don t", so every contraction in the lexicon was dead
    text that could never match. Deleting gives "dont", which is also how people type it when
    their keyboard does not help them. Same for French elision — "c'est" -> "cest".
    """
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _APOS.sub("", s.lower())
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


class Detection:
    """Why we froze. `evidence` is the matched phrase (lexicon) or the model's reason — it is
    what the operator reads first, so it has to say something."""

    def __init__(self, fired, kind="", evidence="", source=""):
        self.fired = bool(fired)
        self.kind = kind
        self.evidence = evidence
        self.source = source        # "lexicon" | "classifier"

    def to_dict(self):
        return {"fired": self.fired, "kind": self.kind,
                "evidence": self.evidence, "source": self.source}

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Detection(%r, %r, %r, %r)" % (self.fired, self.kind, self.evidence, self.source)


NONE = Detection(False)


def check_lexicon(text):
    """Deterministic pass. Returns a Detection; never raises, never calls out."""
    norm = normalize(text)
    if not norm:
        return NONE
    # Whole-message matches first: they are the least ambiguous signal we have.
    for kind, phrases in _EXACT.items():
        if norm in phrases:
            return Detection(True, kind, norm, "lexicon")
    # Most actionable kind wins when a message trips more than one.
    for kind in (WRONG_NUMBER, HOSTILE, UNSUBSCRIBE):
        for phrase in _CONTAINS.get(kind, []):
            if phrase in norm:
                return Detection(True, kind, phrase, "lexicon")
    return NONE


_SYSTEM = """You classify ONE inbound WhatsApp message sent to an automated health-coaching
assistant. Decide only whether the sender is objecting to being contacted at all.

Objecting means: they say they are the wrong person, they do not know us, they never signed up,
they want the messages to stop, or they are hostile about being contacted.

NOT objecting: declining a suggestion, saying they are busy, saying they want to stop a habit
(smoking, snacking), being negative about their progress, or any ordinary coaching reply.

Reply with JSON only: {"objection": true|false, "kind": "wrong_number"|"unsubscribe"|"hostile",
"reason": "<a few words quoting the message>"}. Use kind "" when objection is false."""


def classify(text, generate=None):
    """Model pass for the phrasings a list cannot hold. Returns a Detection or NONE.

    Never raises: the gate must survive a rate-limited or broken model, and the lexicon is
    already carrying the explicit cases. A failure here simply means no ADDITIONAL detection.
    """
    body = str(text or "").strip()
    if not body:
        return NONE
    try:
        if generate is None:
            import gateway
            generate = gateway.generate
    except Exception as e:  # noqa: BLE001 - no gateway here means no second opinion, nothing more
        print("WARN objection classifier unavailable: %s" % type(e).__name__)
        return NONE
    gen = generate
    try:
        result = gen([{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": body[:1500]}], json_mode=True)
        data = json.loads((result or {}).get("text") or "{}")
    except Exception as e:  # noqa: BLE001 - advisory layer; the floor still holds
        print("WARN objection classifier unavailable: %s" % type(e).__name__)
        return NONE
    if not isinstance(data, dict) or not data.get("objection"):
        return NONE
    kind = data.get("kind") if data.get("kind") in (WRONG_NUMBER, UNSUBSCRIBE, HOSTILE) else UNSUBSCRIBE
    return Detection(True, kind, str(data.get("reason") or "")[:200], "classifier")


def detect(text, use_model=True, generate=None):
    """Full gate. Lexicon first — a hit short-circuits, so the common case costs no tokens."""
    hit = check_lexicon(text)
    if hit.fired:
        return hit
    if not use_model:
        return NONE
    return classify(text, generate=generate)
