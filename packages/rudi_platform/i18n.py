"""
MEET_RUDI platform — the strings we author ourselves, in every clone's languages.

The model writes its replies in the person's language; this catalogue holds only the fixed,
non-model messages: the first greeting, welcome-back, media acknowledgement, error and rest
notes, the keep-warm nudge, the frozen-thread acknowledgement, the leak-guard redirect.

Ported from services/whatsapp/src/i18n.py on 2026-10-29 with one change: the persona's name is
the `{persona}` placeholder instead of a hard-coded "Rudi", so a white-label clone greets as
itself. tests/test_parity.py holds the base strings to the live copy (with persona="Rudi").

Each string key declares the placeholders it may use. A clone version can override any string
per locale (settings["strings"] = {key: {locale: text}}); `Catalogue` checks overrides when the
version is built, so a typo'd placeholder fails at publish rather than in a person's chat.

Lookup order for one key: the clone's override in the person's language, the platform text in
that language, then the same two for the clone's default language, then English.
"""

from __future__ import annotations

import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

DEFAULT_LOCALE = "en"


class I18nError(ValueError):
    """An unknown key, a bad override, or a placeholder without a value."""


@dataclass(frozen=True)
class StringSpec:
    key: str
    placeholders: frozenset[str]
    purpose: str


SPECS: dict[str, StringSpec] = {
    s.key: s
    for s in (
        StringSpec(
            "intro", frozenset({"persona"}), "First message to a brand-new contact"
        ),
        StringSpec(
            "welcome_back", frozenset({"persona"}), "Returning contact, new session"
        ),
        StringSpec(
            "media_ack",
            frozenset({"persona", "kind"}),
            "Media the clone cannot read yet",
        ),
        StringSpec("tired", frozenset({"persona"}), "All models rate-limited"),
        StringSpec("error", frozenset({"persona"}), "A turn failed"),
        StringSpec(
            "nudge", frozenset({"persona"}), "Proactive keep-warm message, in window"
        ),
        StringSpec(
            "generic_goal", frozenset(), "Template variable when no goal is stored"
        ),
        StringSpec(
            "frozen_ack", frozenset({"persona"}), "Sent once when a thread is frozen"
        ),
        StringSpec(
            "leak_redirect",
            frozenset({"persona"}),
            "Replaces a reply the leak guard blocked",
        ),
    )
}

PLATFORM_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "intro": "👋 Hi, nice to meet you! I am {persona}. I help people achieve their goals. What's your name and how can I help you?",
        "welcome_back": "👋 Hey again! How are things rolling for you? What can we move forward today?",
        "media_ack": "Thanks, I got your {kind}! 👍 I'll be able to look at these properly soon.",
        "tired": "😴 I'm resting for a bit — message me again in a little while!",
        "error": "Sorry — I had a hiccup. Please try again in a moment. 💬",
        "nudge": "👋 Just checking in — how's it going? A quick reply keeps us moving together 🙂",
        "generic_goal": "our agreed goal",
        "frozen_ack": "Sorry for the interruption — I've stopped messaging you. Someone from our team will check how this number reached us. You won't hear from me again.",
        "leak_redirect": "Let's keep the focus on you. How are things going with what you're working on?",
    },
    "de": {
        "intro": "👋 Hallo, schön dich kennenzulernen! Ich bin {persona}. Ich helfe Menschen, ihre Ziele zu erreichen. Wie heißt du und wie kann ich dir helfen?",
        "welcome_back": "👋 Schön, dass du wieder da bist! Wie läuft es bei dir? Was möchtest du heute angehen?",
        "media_ack": "Danke, ich habe deine Nachricht erhalten! 👍 Bald kann ich mir so etwas richtig ansehen.",
        "tired": "😴 Ich mache gerade eine kurze Pause — schreib mir gleich noch einmal!",
        "error": "Entschuldige — da ist etwas schiefgelaufen. Bitte versuch es gleich noch einmal. 💬",
        "nudge": "👋 Ich schau nur kurz vorbei — wie läuft's? Eine kurze Antwort hält uns gemeinsam in Schwung 🙂",
        "generic_goal": "unserem vereinbarten Ziel",
        "frozen_ack": "Entschuldige die Störung — ich schreibe dir nicht mehr. Jemand aus unserem Team prüft, wie diese Nummer zu uns gelangt ist. Du hörst nichts mehr von mir.",
        "leak_redirect": "Lass uns bei dir bleiben. Wie läuft es mit dem, woran du gerade arbeitest?",
    },
    "fr": {
        "intro": "👋 Bonjour, ravi de te rencontrer ! Je suis {persona}. J'aide les gens à atteindre leurs objectifs. Comment t'appelles-tu et comment puis-je t'aider ?",
        "welcome_back": "👋 Content de te revoir ! Comment ça se passe pour toi ? Qu'est-ce qu'on fait avancer aujourd'hui ?",
        "media_ack": "Merci, j'ai bien reçu ton message ! 👍 Je pourrai bientôt regarder cela comme il faut.",
        "tired": "😴 Je me repose un petit instant — réécris-moi dans un moment !",
        "error": "Désolé — j'ai eu un petit souci. Réessaie dans un instant, s'il te plaît. 💬",
        "nudge": "👋 Je passe juste prendre des nouvelles — comment ça va ? Un petit mot et on continue ensemble 🙂",
        "generic_goal": "notre objectif",
        "frozen_ack": "Désolé pour le dérangement — je ne t'écrirai plus. Quelqu'un de notre équipe va vérifier comment ce numéro est arrivé chez nous. Tu n'auras plus de messages de ma part.",
        "leak_redirect": "Restons concentrés sur toi. Comment ça avance de ton côté ?",
    },
    "nl": {
        "intro": "👋 Hoi, leuk je te ontmoeten! Ik ben {persona}. Ik help mensen hun doelen te bereiken. Hoe heet je en hoe kan ik je helpen?",
        "welcome_back": "👋 Fijn dat je er weer bent! Hoe gaat het met je? Wat gaan we vandaag vooruithelpen?",
        "media_ack": "Bedankt, ik heb je bericht ontvangen! 👍 Binnenkort kan ik hier goed naar kijken.",
        "tired": "😴 Ik rust even uit — stuur me zo nog een berichtje!",
        "error": "Sorry — er ging even iets mis. Probeer het zo meteen opnieuw. 💬",
        "nudge": "👋 Ik check even in — hoe gaat het? Een kort berichtje houdt ons samen op gang 🙂",
        "generic_goal": "ons afgesproken doel",
        "frozen_ack": "Sorry voor de storing — ik stuur je geen berichten meer. Een collega kijkt na hoe dit nummer bij ons terechtgekomen is. Je hoort niets meer van mij.",
        "leak_redirect": "Laten we de focus bij jou houden. Hoe gaat het met waar je mee bezig bent?",
    },
}  # fmt: skip


def normalize_locale(code: object) -> str | None:
    """An incoming language code reduced to the 2–3 letter key the catalogue uses, or None."""
    if not isinstance(code, str):
        return None
    c = code.strip().lower().replace("_", "-").split("-")[0]
    return c if 2 <= len(c) <= 3 and c.isalpha() else None


def placeholders_in(text: str) -> set[str]:
    """The {names} a template uses; I18nError for positional or malformed fields."""
    found: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as e:
        raise I18nError(f"malformed template: {e}") from None
    for _, field, _, _ in parsed:
        if field is None:
            continue
        if not field.isidentifier():
            raise I18nError(f"placeholders must be named, got {{{field}}}")
        found.add(field)
    return found


def _check_template(key: str, text: str) -> None:
    spec = SPECS.get(key)
    if spec is None:
        raise I18nError(f"unknown string key {key!r}")
    if not isinstance(text, str) or not text.strip():
        raise I18nError(f"{key}: text must be a non-empty string")
    extra = placeholders_in(text) - spec.placeholders
    if extra:
        allowed = ", ".join(sorted(spec.placeholders)) or "none"
        raise I18nError(
            f"{key}: unknown placeholders {sorted(extra)} (allowed: {allowed})"
        )


class Catalogue:
    """Platform strings plus one clone version's overrides."""

    def __init__(
        self,
        overrides: Mapping[str, Mapping[str, str]] | None = None,
        default_locale: str = DEFAULT_LOCALE,
        base: Mapping[str, Mapping[str, str]] = PLATFORM_STRINGS,
    ) -> None:
        self._base = base
        self._overrides: dict[str, dict[str, str]] = {}
        loc = normalize_locale(default_locale)
        if loc is None:
            raise I18nError(f"bad default locale {default_locale!r}")
        self.default_locale = loc
        for key, per_locale in (overrides or {}).items():
            if not isinstance(per_locale, Mapping):
                raise I18nError(f"{key}: overrides must map locale to text")
            for raw_locale, text in per_locale.items():
                code = normalize_locale(raw_locale)
                if code is None:
                    raise I18nError(f"{key}: bad locale {raw_locale!r}")
                _check_template(key, text)
                self._overrides.setdefault(key, {})[code] = text

    def _template(self, key: str, locale: str | None) -> str:
        if key not in SPECS:
            raise I18nError(f"unknown string key {key!r}")
        chain = [normalize_locale(locale), self.default_locale, DEFAULT_LOCALE]
        for code in chain:
            if code is None:
                continue
            if code in self._overrides.get(key, {}):
                return self._overrides[key][code]
            if key in self._base.get(code, {}):
                return self._base[code][key]
        raise I18nError(f"no text for {key!r} in any fallback language")

    def text(self, key: str, locale: str | None = None, **values: object) -> str:
        """The string for `key` in the person's language, with its placeholders filled.

        Every placeholder the chosen template uses must be given; extra values are ignored, so
        callers can always pass persona= and kind= without knowing the language.
        """
        template = self._template(key, locale)
        needed = placeholders_in(template)
        missing = needed - set(values)
        if missing:
            raise I18nError(f"{key}: no value for {sorted(missing)}")
        return template.format(**{k: values[k] for k in needed})

    def missing(self, locales: Iterable[str]) -> dict[str, list[str]]:
        """Keys with no text in a locale (neither override nor platform), per locale.

        For publish checks: a clone serving Polish needs every key in Polish, or it will greet
        Polish speakers in English.
        """
        out: dict[str, list[str]] = {}
        for raw in locales:
            code = normalize_locale(raw)
            if code is None:
                raise I18nError(f"bad locale {raw!r}")
            gaps = [
                k
                for k in SPECS
                if code not in self._overrides.get(k, {})
                and k not in self._base.get(code, {})
            ]
            if gaps:
                out[code] = gaps
        return out
