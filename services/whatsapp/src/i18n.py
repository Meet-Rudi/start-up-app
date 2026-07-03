"""
MEET_RUDI — i18n scaffold for WhatsApp canned strings.

Locale lives on the user profile (ContactMeta.locale, "last used language"). Fixed, non-LLM
messages (intro, welcome-back, media ack, errors) are keyed by locale here and resolved with
`t()`, which falls back to English. English is filled; add "de"/"fr"/"nl" tables as content
lands — no code change needed (CLAUDE.md §3: i18n-ready, English-first).

Rudi's free-form replies are written by the model in the user's own language; this catalog is
only for the strings we author ourselves.
"""

DEFAULT_LOCALE = "en"

STRINGS = {
    "en": {
        # First contact ever (brand-new number). New numbers default to English — we haven't
        # seen the user's language yet.
        "intro": ("👋 Hi, nice to meet you! I am Rudi. I help people achieve their goals. "
                  "What's your name and how can I help you?"),
        # Returning number starting a fresh session (previous one concluded).
        "welcome_back": ("👋 Hey again! How are things rolling for you? "
                         "What can we move forward today?"),
        "media_ack": "Thanks, I got your {kind}! 👍 I'll be able to look at these properly soon.",
        "tired": "😴 I'm resting for a bit — message me again in a little while!",
        "error": "Sorry — I had a hiccup. Please try again in a moment. 💬",
        # Proactive keep-warm nudge (free-form, in-window) — invites a reply to reset the window.
        "nudge": "👋 Just checking in — how's it going? A quick reply keeps us moving together 🙂",
    },
    "de": {
        "intro": ("👋 Hallo, schön dich kennenzulernen! Ich bin Rudi. Ich helfe Menschen, ihre "
                  "Ziele zu erreichen. Wie heißt du und wie kann ich dir helfen?"),
        "welcome_back": ("👋 Schön, dass du wieder da bist! Wie läuft es bei dir? "
                         "Was möchtest du heute angehen?"),
        "media_ack": "Danke, ich habe deine Nachricht erhalten! 👍 Bald kann ich mir so etwas richtig ansehen.",
        "tired": "😴 Ich mache gerade eine kurze Pause — schreib mir gleich noch einmal!",
        "error": "Entschuldige — da ist etwas schiefgelaufen. Bitte versuch es gleich noch einmal. 💬",
        "nudge": "👋 Ich schau nur kurz vorbei — wie läuft's? Eine kurze Antwort hält uns gemeinsam in Schwung 🙂",
    },
    "fr": {
        "intro": ("👋 Bonjour, ravi de te rencontrer ! Je suis Rudi. J'aide les gens à atteindre "
                  "leurs objectifs. Comment t'appelles-tu et comment puis-je t'aider ?"),
        "welcome_back": ("👋 Content de te revoir ! Comment ça se passe pour toi ? "
                         "Qu'est-ce qu'on fait avancer aujourd'hui ?"),
        "media_ack": "Merci, j'ai bien reçu ton message ! 👍 Je pourrai bientôt regarder cela comme il faut.",
        "tired": "😴 Je me repose un petit instant — réécris-moi dans un moment !",
        "error": "Désolé — j'ai eu un petit souci. Réessaie dans un instant, s'il te plaît. 💬",
        "nudge": "👋 Je passe juste prendre des nouvelles — comment ça va ? Un petit mot et on continue ensemble 🙂",
    },
    "nl": {
        "intro": ("👋 Hoi, leuk je te ontmoeten! Ik ben Rudi. Ik help mensen hun doelen te "
                  "bereiken. Hoe heet je en hoe kan ik je helpen?"),
        "welcome_back": ("👋 Fijn dat je er weer bent! Hoe gaat het met je? "
                         "Wat gaan we vandaag vooruithelpen?"),
        "media_ack": "Bedankt, ik heb je bericht ontvangen! 👍 Binnenkort kan ik hier goed naar kijken.",
        "tired": "😴 Ik rust even uit — stuur me zo nog een berichtje!",
        "error": "Sorry — er ging even iets mis. Probeer het zo meteen opnieuw. 💬",
        "nudge": "👋 Ik check even in — hoe gaat het? Een kort berichtje houdt ons samen op gang 🙂",
    },
}


def normalize_locale(code):
    """Reduce an incoming language code to a 2-letter key we key the catalog by, or None."""
    if not isinstance(code, str):
        return None
    c = code.strip().lower().replace("_", "-").split("-")[0]
    return c if 2 <= len(c) <= 3 else None


def t(key, locale=DEFAULT_LOCALE, **kw):
    """Resolve a string for `locale`, falling back to English, then to the key itself."""
    table = STRINGS.get(locale) or STRINGS[DEFAULT_LOCALE]
    s = table.get(key)
    if s is None:
        s = STRINGS[DEFAULT_LOCALE].get(key, key)
    return s.format(**kw) if kw else s
