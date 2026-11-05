"""objection-gate: stop on a wrong number, an opt-out or hostility, before any model call
(Baseline; the inbound mirror of the reply gate).

Ported from services/whatsapp/src/objection.py (the deterministic layer) and held to it by
tests/test_engine_parity.py. Every locale's word list is scanned, because on a wrong number the
locale on file is exactly the field we cannot trust. The model classifier that catches what a
word list cannot (first five messages of a thread) becomes a turn skill with the turn-skill
runtime; until then CM's processor keeps running it.

A halt is a freeze: the engine answers with the clone's `frozen_ack` string once, and the
channel marks the conversation frozen for an operator.
"""

import re
import unicodedata

from rudi_platform.engine.skill import RealTimeSkill
from rudi_platform.skills.contract import SkillRequest, SkillResult

WRONG_NUMBER = "wrong_number"
UNSUBSCRIBE = "unsubscribe"
HOSTILE = "hostile"

# Matched against the WHOLE normalised message: objections on their own, ordinary words inside a
# sentence ("ik wil stoppen met roken").
EXACT = {
    WRONG_NUMBER: ["wie", "wie is dit", "who dis", "qui"],
    UNSUBSCRIBE: [
        "stop", "stopp", "stop stop", "afmelden", "uitschrijven", "stopzetten",
        "unsubscribe", "remove me", "opt out", "optout",
        "desabonner", "desinscription", "arret", "arretez",
        "abmelden", "abbestellen", "aufhoren",
    ],
}  # fmt: skip

# Matched anywhere. Written as they look after normalize(): no apostrophes, no accents.
CONTAINS = {
    WRONG_NUMBER: [
        "verkeerd nummer", "verkeerde nummer", "verkeerde persoon", "verkeerd persoon",
        "wie is dit", "wie ben jij", "wie bent u", "wie zijt gij",
        "ik ken u niet", "ik ken je niet", "ken ik u niet", "ik ken jou niet",
        "niet de juiste persoon", "je hebt de verkeerde", "u hebt de verkeerde",
        "ik heb me niet ingeschreven", "ik heb me nooit ingeschreven",
        "nooit aangemeld", "nooit ingeschreven", "ik heb hier niet om gevraagd",
        "wrong number", "wrong person", "who is this", "who are you",
        "i dont know you", "i do not know you",
        "i didnt sign up", "i never signed up",
        "i never registered", "i didnt register", "i did not ask for this",
        "you have the wrong", "not the right person", "this isnt me", "this is not me",
        "mauvais numero", "mauvaise personne", "qui etes vous", "qui est ce",
        "je ne vous connais pas", "je ne me suis pas inscrit", "je nai rien demande",
        "falsche nummer", "falsche person", "wer ist das", "wer sind sie",
        "ich kenne sie nicht", "ich habe mich nicht angemeldet", "nie angemeldet",
    ],
    UNSUBSCRIBE: [
        "geen berichten meer", "stuur me geen", "stuur mij geen", "niet meer contacteren",
        "stop met mij", "stop met berichten", "haal me van", "schrijf me uit",
        "no more messages", "stop contacting", "stop messaging", "stop texting",
        "dont message me", "do not contact", "take me off",
        "ne me contactez plus", "plus de messages",
        "arretez de me contacter", "arretez de menvoyer", "arretez de mecrire",
        "keine nachrichten mehr", "nicht mehr kontaktieren",
    ],
    HOSTILE: [
        "laat me met rust", "hou op", "houd op", "laat mij met rust", "val me niet lastig",
        "dit is spam", "is dit spam", "naar de politie", "mijn advocaat",
        "klacht indienen", "een klacht in", "juridische stappen",
        "leave me alone", "stop bothering", "harassment", "harassing",
        "this is spam", "report you", "reporting you", "my lawyer", "the police",
        "laissez moi tranquille", "cest du spam", "porter plainte", "harcelement",
        "lassen sie mich in ruhe", "belastigung", "das ist spam", "anzeige erstatten",
    ],
}  # fmt: skip

_APOS = re.compile(r"['‘’ʼ`]")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize(text: object) -> str:
    """Lowercase, no diacritics, apostrophes deleted ("don't" → "dont"), punctuation to spaces."""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _APOS.sub("", s.lower())
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def check(text: object) -> dict[str, str] | None:
    """{kind, evidence, source} when the message objects, else None. Never raises."""
    norm = normalize(text)
    if not norm:
        return None
    for kind, phrases in EXACT.items():
        if norm in phrases:
            return {"kind": kind, "evidence": norm, "source": "lexicon"}
    for kind in (WRONG_NUMBER, HOSTILE, UNSUBSCRIBE):  # most actionable kind first
        for phrase in CONTAINS[kind]:
            if phrase in norm:
                return {"kind": kind, "evidence": phrase, "source": "lexicon"}
    return None


class Skill(RealTimeSkill):
    def on_inbound(self, req: SkillRequest) -> SkillResult:
        hit = check(req.input.get("text"))
        return SkillResult(output={"halt": hit}) if hit else SkillResult()
