"""
MEET_RUDI — de-identification helpers (§0.1 Prime Directive, §5 Data & Privacy).

TWO TIERS, deliberately different in reversibility. Getting this distinction right is the whole
design; collapsing them into one "PII scrubber" is what makes such systems either leaky or useless.

  TIER 1 — REDACT (irreversible).  Direct identifiers: Belgian national number (RRN), email, IBAN,
  payment card, phone. Applied at INGEST, before ANY persistence, on every input channel (WhatsApp
  webhook, test console, ASR transcript, consent intake). No key is kept anywhere: the original is
  unrecoverable by design, so it cannot be stored, exported, logged, or subpoenaed out of us. A
  redacted value is GONE.

  TIER 2 — PSEUDONYMIZE (reversible, session-scoped).  Quasi-identifiers that make a person
  indirectly identifiable: other people's names, places, employers, clinicians. These are NOT
  redacted at ingest, because Rudi's replies must be able to say "Peter" back to the user. Instead
  the raw text stays inside the AWS-EU plane (permitted by §0.1) and is masked ONLY on the wire to
  the model gateway, then unmasked in the reply before it reaches the send channel.

        inbound ──redact(tier1)──▶ store (AWS EU, raw names)
                                     │
                                     ├── vault.mask(tier2) ──▶ MODEL (3rd-party / GPU plane)
                                     │                            │
                        WhatsApp ◀── vault.unmask ◀───────────────┘

The Tier-2 alias map lives in an `AliasVault`, held for the duration of one conversation session
and dropped when the session concludes (§5: minimize retention). Durable memory never stores names
— it stores the alias plus a non-identifying ROLE descriptor ("Person_A = running partner"), so a
later session can still say "your running partner" with the vault long gone. See
docs/pii-and-memory-blueprint.md.

Person detection is behind the `PersonDetector` seam (§2: everything swappable behind an
interface). The bundled `HeuristicDetector` is gazetteer + grammar based — zero dependencies, runs
in Lambda, tuned for precision over recall. Swap in spaCy/Presidio or a self-hosted NER on the GPU
plane without touching callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------------- placeholders
# Wire format is exactly "<| label |>". Models reliably copy it through, and the surrounding pipes
# survive tokenization better than bare angle brackets. Matching tolerates whitespace drift because
# models DO reformat "<|Person_A|>" into "<| Person_A |>" and back.
PLACEHOLDER_FMT = "<| %s |>"
PLACEHOLDER_RE = re.compile(r"<\s*\|\s*([A-Za-z0-9_\-]{1,40})\s*\|\s*>")

# Tier-1 labels (irreversible).
LBL_NATIONAL_ID = "users-national-id"
LBL_EMAIL = "email-address"
LBL_PHONE = "phone-number"
LBL_IBAN = "bank-account"
LBL_CARD = "payment-card"


# ------------------------------------------------------------------------------ tier 1: redaction
def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _rrn_ok(d: str) -> bool:
    """Validate an 11-digit Belgian Rijksregisternummer / Numéro de Registre National.

    Layout YY MM DD SSS CC: birth date, sequence (odd=male, even=female), mod-97 checksum. The
    checksum is what makes this safe to auto-redact — a bare \\d{11} regex would eat phone numbers,
    order references and amounts. Two variants exist: born <2000 checks the 9 digits as-is; born
    ≥2000 prefixes a "2" before taking mod 97. We accept either, since we cannot know the century.
    """
    base, check = d[:9], int(d[9:])
    if not 1 <= check <= 97:
        return False
    if check == 97 - (int(base) % 97):
        return True
    return check == 97 - (int("2" + base) % 97)


def _rrn_plausible_date(dd: str, mm: str, day: str) -> bool:
    """Loose date sanity. Month may carry +20 or +40 (bis-nummer: non-residents / unknown DOB),
    and day/month 00 is legal when the birth date was never established."""
    m, day_i = int(mm), int(day)
    if not (m == 0 or 1 <= m <= 12 or 21 <= m <= 32 or 41 <= m <= 52):
        return False
    return 0 <= day_i <= 31


# yy.mm.dd-sss.cc with any/no separators — covers "85.07.30-033.28", "85073003328", "850730 033 28".
_RRN_RE = re.compile(r"(?<!\d)(\d{2})[.\-/ ]?(\d{2})[.\-/ ]?(\d{2})[.\-/ ]?(\d{3})[.\-/ ]?(\d{2})(?!\d)")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
# Conservative: only clear international (+32…, 0032…) or Belgian mobile (04xx) forms. Anything
# looser starts eating dates, weights and step counts, which breaks the conversation for no gain.
_PHONE_RE = re.compile(r"(?<![\w])(?:\+|00)\d{1,3}[\s.\-/]?(?:\d[\s.\-/]?){6,13}\d|(?<![\w\d])04\d{2}[\s.\-/]?(?:\d[\s.\-/]?){5,7}\d(?![\w])")
_IBAN_RE = re.compile(r"\b([A-Z]{2})(\d{2})((?:[ ]?[A-Z0-9]){10,30})\b")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _iban_ok(country: str, check: str, rest: str) -> bool:
    body = (country + check + re.sub(r"\s", "", rest)).upper()
    rearranged = body[4:] + body[:4]
    n = 0
    for ch in rearranged:
        n = n * (100 if ch.isalpha() else 10) + (ord(ch) - 55 if ch.isalpha() else ord(ch) - 48)
    return n % 97 == 1


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Strip direct identifiers IRREVERSIBLY. Returns (clean_text, {label: count}).

    Call this at every ingress, before the message is written anywhere — including before the
    operator console can see it and before it is logged. Order matters: RRN runs first, because an
    11-digit national number also matches the phone and card patterns.
    """
    found: dict[str, int] = {}

    def _hit(label: str) -> str:
        found[label] = found.get(label, 0) + 1
        return PLACEHOLDER_FMT % label

    def _sub_rrn(m: re.Match) -> str:
        digits = "".join(m.groups())
        if not _rrn_plausible_date(*m.groups()[:3]) or not _rrn_ok(digits):
            return m.group(0)          # not a national number — leave it alone
        return _hit(LBL_NATIONAL_ID)

    def _sub_iban(m: re.Match) -> str:
        return _hit(LBL_IBAN) if _iban_ok(*m.groups()) else m.group(0)

    def _sub_card(m: re.Match) -> str:
        return _hit(LBL_CARD) if _luhn_ok(re.sub(r"[^\d]", "", m.group(0))) else m.group(0)

    out = _RRN_RE.sub(_sub_rrn, text or "")
    out = _EMAIL_RE.sub(lambda m: _hit(LBL_EMAIL), out)
    out = _IBAN_RE.sub(_sub_iban, out)
    out = _CARD_RE.sub(_sub_card, out)
    out = _PHONE_RE.sub(lambda m: _hit(LBL_PHONE), out)
    return out, found


# --------------------------------------------------------------- tier 2: person detection (seam)
class PersonDetector:
    """Seam (§2). Returns the person-name surface forms found in `text`, longest first."""

    def find(self, text: str, locale: str = "en") -> list[str]:
        raise NotImplementedError


# Words that are capitalized mid-sentence but are not people. Deliberately small: the gazetteer +
# trigger grammar carry the precision, and an over-long stoplist hides detector failures.
_NOT_NAMES = {
    "I", "I'm", "Im", "Ok", "Okay", "Yes", "No", "Hi", "Hey", "Hello", "Thanks", "Rudi",
    "God", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "Covid", "Whatsapp", "WhatsApp", "Christmas", "Easter",
}

# The `regex` module (\p{L}) is not in the Lambda runtime, so spell out Latin-1 letter ranges —
# enough for NL/FR/EN/DE names in the Belgian pilot. A real NER model replaces this entirely.
_U = "A-ZÀ-ÖØ-Þ"                       # uppercase incl. accented
_NAME = "[%s][a-zà-öø-ÿ%s'’-]{1,20}" % (_U, _U)   # Peter, Jean-Luc, O'Brien, Van Herck

_RELATIONS = (
    "friend|wife|husband|partner|brother|sister|mother|father|mum|mom|dad|son|daughter|colleague|"
    "boss|neighbou?r|doctor|nurse|coach|vriend|vriendin|man|vrouw|broer|zus|moeder|vader|collega|"
    "dokter|ami|amie|femme|mari|frère|soeur|mère|père|collègue|médecin"
)
# Titles are consumed but NOT captured, so "with Doctor Smith" masks Smith and keeps the title —
# otherwise the title is taken for the name and the real name leaks through untouched.
_TITLE = (r"(?:(?i:doctor|dr|mr|mrs|ms|miss|prof|dokter|dhr|mevr|mevrouw|meneer|"
          r"docteur|madame|monsieur|mme|mlle)\.?\s+)?")

# Trigger grammar: a capitalized token in one of these frames is a person with high confidence,
# even when the name is in no gazetteer (crucial for Belgium's mix of name origins).
# NOTE: case-insensitivity is scoped to the trigger words only — applying re.I to the whole pattern
# would make [A-Z] match lowercase, and the frame would swallow the next ordinary word ("my wife to
# the hospital" → "to" detected as a person).
_TRIGGER_RES = [
    re.compile(r"(?i:with|met|avec|samen met)\s+%s(?P<n>%s)" % (_TITLE, _NAME), re.UNICODE),
    re.compile(r"(?i:my|mijn|mon|ma)\s+(?i:%s)\s+%s(?P<n>%s)" % (_RELATIONS, _TITLE, _NAME),
               re.UNICODE),
    re.compile(r"(?P<n>%s)\s+(?i:said|told|asked|came|went|helped|zei|vertelde|ging|kwam)" % _NAME,
               re.UNICODE),
]
_CAP_RE = re.compile(r"\b(%s)\b" % _NAME, re.UNICODE)


@dataclass
class HeuristicDetector(PersonDetector):
    """Zero-dependency detector: trigger grammar + optional gazetteer, precision-biased.

    Precision over recall is the right bias here for one reason: a false positive silently mangles
    a real word into "<| Person_A |>" and Rudi answers nonsense, which the user sees. A false
    negative leaks one first name to the model — bad, but bounded, and caught by the eval suite
    (§8) rather than by the user. Upgrade path is a real NER model behind this same interface.
    """

    gazetteer: set[str] = field(default_factory=set)   # known first names, lowercased

    def find(self, text: str, locale: str = "en") -> list[str]:
        hits: set[str] = set()
        for rx in _TRIGGER_RES:
            for m in rx.finditer(text or ""):
                cand = m.group("n")
                if cand not in _NOT_NAMES:
                    hits.add(cand)
        if self.gazetteer:
            for m in _CAP_RE.finditer(text or ""):
                cand = m.group(1)
                if cand.lower() in self.gazetteer and cand not in _NOT_NAMES:
                    hits.add(cand)
        return sorted(hits, key=len, reverse=True)      # longest first: "Jan Peeters" before "Jan"


# ----------------------------------------------------------------------------- tier 2: the vault
@dataclass
class AliasVault:
    """Reversible, session-scoped name↔alias map.

    Persisted separately from the transcript (conversations/{uid}/aliases.json) so that erasure and
    TTL are a single object delete, and so it never rides along in an export. Dropped when the
    session concludes; `roles` is what survives into durable memory.
    """

    mapping: dict[str, str] = field(default_factory=dict)    # "Peter" -> "Person_A"
    roles: dict[str, str] = field(default_factory=dict)      # "Person_A" -> "running partner"
    prefix: str = "Person"

    def _next_alias(self) -> str:
        n = len(self.mapping)
        # A, B, ... Z, AA, AB, ... — no practical ceiling, still short in the prompt.
        letters = ""
        while True:
            letters = chr(65 + n % 26) + letters
            n = n // 26 - 1
            if n < 0:
                break
        return "%s_%s" % (self.prefix, letters)

    def alias_for(self, name: str) -> str:
        if name not in self.mapping:
            self.mapping[name] = self._next_alias()
        return self.mapping[name]

    def mask(self, text: str, detector: PersonDetector, locale: str = "en") -> str:
        """Replace known and newly-detected names with placeholders.

        Known aliases are applied first and unconditionally, so a name introduced three turns ago
        stays consistently masked even in a turn where the detector would not fire on it.
        """
        out = text or ""
        for name in sorted(self.mapping, key=len, reverse=True):
            out = re.sub(r"\b%s\b" % re.escape(name), PLACEHOLDER_FMT % self.mapping[name], out)
        for name in detector.find(out, locale):
            out = re.sub(r"\b%s\b" % re.escape(name), PLACEHOLDER_FMT % self.alias_for(name), out)
        return out

    def unmask(self, text: str, fallback: str = "") -> str:
        """Swap placeholders back to real names, just before the send channel.

        Any placeholder we cannot resolve — a Tier-1 redaction the model echoed back, a hallucinated
        alias, or one whose vault entry has expired — is replaced with its role descriptor if we
        have one, else `fallback` (default: drop it). A raw "<| … |>" must NEVER reach the user.
        """
        reverse = {alias: name for name, alias in self.mapping.items()}

        def _swap(m: re.Match) -> str:
            label = m.group(1)
            if label in reverse:
                return reverse[label]
            if label in self.roles:
                return self.roles[label]
            return fallback

        return PLACEHOLDER_RE.sub(_swap, text or "")

    def set_role(self, alias: str, role: str) -> None:
        """Attach a non-identifying descriptor to an alias, for durable memory."""
        if alias in self.mapping.values() and role:
            self.roles[alias] = role.strip()[:40]

    def to_dict(self) -> dict:
        return {"mapping": self.mapping, "roles": self.roles, "prefix": self.prefix}

    @classmethod
    def from_dict(cls, d: dict | None) -> "AliasVault":
        d = d or {}
        return cls(mapping=dict(d.get("mapping") or {}), roles=dict(d.get("roles") or {}),
                   prefix=d.get("prefix") or "Person")


def has_placeholder(text: str) -> bool:
    """True if any unresolved placeholder survives — assert this is False before every send."""
    return bool(PLACEHOLDER_RE.search(text or ""))
