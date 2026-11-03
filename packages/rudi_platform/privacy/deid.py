"""
MEET_RUDI platform — de-identification (Prime Directive 1, CLAUDE.md §5).

Ported from services/whatsapp/src/deid.py on 2026-10-28. The WhatsApp and call services keep
their own copies until they are cut over to the platform (conventions §6); change behaviour in
both places until then.

TWO TIERS, deliberately different in reversibility:

  TIER 1 — REDACT (irreversible). Direct identifiers: Belgian national number (RRN), email, IBAN,
  payment card, phone. Applied at INGEST, before any persistence. No key is kept: a redacted value
  is gone. The national-number rule is Belgian because tenant #1 is; other countries' identifiers
  are added as tenants outside Belgium arrive.

  TIER 2 — PSEUDONYMISE (reversible, session-scoped). Names of other people. The raw text stays in
  the AWS EU plane and is masked only on the wire to a model, then restored before a person reads
  the reply. The platform's ModelGateway applies this sandwich itself, so no caller can skip it.

Platform changes from the service copy:
  - "Rudi" is no longer hard-coded as a non-person: a clone's persona names are passed to
    HeuristicDetector(keep=...), so a persona called Emma is never masked as a third party.
  - session_vault() is gone; what a session is depends on the channel and is the engine's call.
  - restore_outbound(redacted=...) takes a str.format pattern ("[{} removed]").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Wire format is exactly "<| label |>". Models reliably copy it through; matching tolerates the
# whitespace drift models introduce ("<|Person_A|>" ↔ "<| Person_A |>").
PLACEHOLDER_RE = re.compile(r"<\s*\|\s*([A-Za-z0-9_\-]{1,40})\s*\|\s*>")

LBL_NATIONAL_ID = "users-national-id"
LBL_EMAIL = "email-address"
LBL_PHONE = "phone-number"
LBL_IBAN = "bank-account"
LBL_CARD = "payment-card"
TIER1_LABELS = frozenset((LBL_NATIONAL_ID, LBL_EMAIL, LBL_PHONE, LBL_IBAN, LBL_CARD))


def placeholder(label: str) -> str:
    return f"<| {label} |>"


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
    """Validate an 11-digit Belgian national number: YY MM DD SSS CC with a mod-97 checksum.

    The checksum is what makes auto-redaction safe; a bare 11-digit pattern would eat phone
    numbers, order references and amounts. Born before 2000 checks the 9 digits as they are; born
    2000 or later prefixes a "2". The century is unknown, so either variant counts.
    """
    base, check = d[:9], int(d[9:])
    if not 1 <= check <= 97:
        return False
    if check == 97 - (int(base) % 97):
        return True
    return check == 97 - (int("2" + base) % 97)


def _rrn_plausible_date(_yy: str, mm: str, day: str) -> bool:
    """Loose date sanity. Month may carry +20 or +40 (bis numbers), and 00 is legal when the birth
    date was never established."""
    m, day_i = int(mm), int(day)
    if not (m == 0 or 1 <= m <= 12 or 21 <= m <= 32 or 41 <= m <= 52):
        return False
    return 0 <= day_i <= 31


_RRN_RE = re.compile(
    r"(?<!\d)(\d{2})[.\-/ ]?(\d{2})[.\-/ ]?(\d{2})[.\-/ ]?(\d{3})[.\-/ ]?(\d{2})(?!\d)"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
# Conservative: clear international (+32…, 0032…) or Belgian mobile (04xx) forms only. Anything
# looser starts eating dates, weights and step counts.
_PHONE_RE = re.compile(
    r"(?<![\w])(?:\+|00)\d{1,3}[\s.\-/]?(?:\d[\s.\-/]?){6,13}\d"
    r"|(?<![\w\d])04\d{2}[\s.\-/]?(?:\d[\s.\-/]?){5,7}\d(?![\w])"
)
_IBAN_RE = re.compile(r"\b([A-Z]{2})(\d{2})((?:[ ]?[A-Z0-9]){10,30})\b")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _iban_ok(country: str, check: str, rest: str) -> bool:
    body = (country + check + re.sub(r"\s", "", rest)).upper()
    rearranged = body[4:] + body[:4]
    n = 0
    for ch in rearranged:
        n = n * (100 if ch.isalpha() else 10) + (
            ord(ch) - 55 if ch.isalpha() else ord(ch) - 48
        )
    return n % 97 == 1


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Strip direct identifiers irreversibly. Returns (clean_text, {label: count}).

    Call at every ingress, before the text is written, logged or shown to an operator. The
    national number runs first because an 11-digit number also matches the phone and card rules.
    """
    found: dict[str, int] = {}

    def _hit(label: str) -> str:
        found[label] = found.get(label, 0) + 1
        return placeholder(label)

    def _sub_rrn(m: re.Match) -> str:
        digits = "".join(m.groups())
        if not _rrn_plausible_date(*m.groups()[:3]) or not _rrn_ok(digits):
            return m.group(0)
        return _hit(LBL_NATIONAL_ID)

    def _sub_iban(m: re.Match) -> str:
        return _hit(LBL_IBAN) if _iban_ok(*m.groups()) else m.group(0)

    def _sub_card(m: re.Match) -> str:
        return (
            _hit(LBL_CARD) if _luhn_ok(re.sub(r"[^\d]", "", m.group(0))) else m.group(0)
        )

    out = _RRN_RE.sub(_sub_rrn, text or "")
    out = _EMAIL_RE.sub(lambda m: _hit(LBL_EMAIL), out)
    out = _IBAN_RE.sub(_sub_iban, out)
    out = _CARD_RE.sub(_sub_card, out)
    out = _PHONE_RE.sub(lambda m: _hit(LBL_PHONE), out)
    return out, found


# --------------------------------------------------------------- tier 2: person detection (seam)
class PersonDetector:
    """Seam: returns the person-name surface forms found in `text`, longest first."""

    def find(self, text: str, locale: str = "en") -> list[str]:
        raise NotImplementedError


# Capitalised mid-sentence but not people. Small on purpose: the trigger grammar and gazetteer
# carry the precision. Persona names are added per clone through HeuristicDetector(keep=...).
NOT_NAMES = frozenset({
    "I", "I'm", "Im", "Ok", "Okay", "Yes", "No", "Hi", "Hey", "Hello", "Thanks",
    "God", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "Covid", "Whatsapp", "WhatsApp", "Christmas", "Easter",
})  # fmt: skip

# The `regex` module (\p{L}) is not in the Lambda runtime, so Latin-1 letter ranges are spelled
# out: enough for NL/FR/EN/DE names. A real NER model replaces this behind the same seam.
_U = "A-ZÀ-ÖØ-Þ"
_NAME = f"[{_U}][a-zà-öø-ÿ{_U}'’-]{{1,20}}"

_RELATIONS = (
    "friend|wife|husband|partner|brother|sister|mother|father|mum|mom|dad|son|daughter|colleague|"
    "boss|neighbou?r|doctor|nurse|coach|vriend|vriendin|man|vrouw|broer|zus|moeder|vader|collega|"
    "dokter|ami|amie|femme|mari|frère|soeur|mère|père|collègue|médecin"
)
# Titles are consumed but not captured, so "with Doctor Smith" masks Smith and keeps the title.
_TITLE = (
    r"(?:(?i:doctor|dr|mr|mrs|ms|miss|prof|dokter|dhr|mevr|mevrouw|meneer|"
    r"docteur|madame|monsieur|mme|mlle)\.?\s+)?"
)

# Case-insensitivity is scoped to the trigger words only: applying it to the whole pattern makes
# [A-Z] match lowercase and the frame swallows ordinary words ("my wife to the hospital").
_TRIGGER_RES = [
    re.compile(rf"(?i:with|met|avec|samen met)\s+{_TITLE}(?P<n>{_NAME})", re.UNICODE),
    re.compile(
        rf"(?i:my|mijn|mon|ma)\s+(?i:{_RELATIONS})\s+{_TITLE}(?P<n>{_NAME})", re.UNICODE
    ),
    re.compile(
        rf"(?P<n>{_NAME})\s+(?i:said|told|asked|came|went|helped|zei|vertelde|ging|kwam)",
        re.UNICODE,
    ),
]
_CAP_RE = re.compile(rf"\b({_NAME})\b", re.UNICODE)


@dataclass
class HeuristicDetector(PersonDetector):
    """Zero-dependency detector: trigger grammar plus optional gazetteer, precision-biased.

    A false positive mangles a real word into a placeholder and the person sees nonsense; a false
    negative leaks one first name to the model, which is bounded and caught by evals. Hence the
    bias. `keep` holds words that are never people in this clone, its persona names above all.
    """

    gazetteer: set[str] = field(default_factory=set)  # known first names, lowercased
    keep: frozenset[str] = frozenset()

    def _skip(self, cand: str) -> bool:
        return cand in NOT_NAMES or cand in self.keep

    def find(self, text: str, locale: str = "en") -> list[str]:
        hits: set[str] = set()
        for rx in _TRIGGER_RES:
            for m in rx.finditer(text or ""):
                cand = m.group("n")
                if not self._skip(cand):
                    hits.add(cand)
        if self.gazetteer:
            for m in _CAP_RE.finditer(text or ""):
                cand = m.group(1)
                if cand.lower() in self.gazetteer and not self._skip(cand):
                    hits.add(cand)
        return sorted(hits, key=len, reverse=True)  # "Jan Peeters" before "Jan"


# ----------------------------------------------------------------------------- tier 2: the vault
@dataclass
class AliasVault:
    """Reversible, session-scoped name ↔ alias map.

    Stored apart from the transcript so erasure and expiry are one delete and it never rides along
    in an export. Dropped when the session ends; `roles` is what survives into durable memory.
    """

    mapping: dict[str, str] = field(default_factory=dict)  # "Peter" -> "Person_A"
    roles: dict[str, str] = field(
        default_factory=dict
    )  # "Person_A" -> "running partner"
    prefix: str = "Person"

    def _next_alias(self) -> str:
        n = len(self.mapping)
        letters = ""
        while True:  # A … Z, AA, AB, …
            letters = chr(65 + n % 26) + letters
            n = n // 26 - 1
            if n < 0:
                break
        return f"{self.prefix}_{letters}"

    def alias_for(self, name: str) -> str:
        if name not in self.mapping:
            self.mapping[name] = self._next_alias()
        return self.mapping[name]

    def mask(self, text: str, detector: PersonDetector, locale: str = "en") -> str:
        """Replace known and newly detected names with placeholders. Known aliases go first and
        unconditionally, so a name from three turns ago stays masked when the detector is silent."""
        out = text or ""
        for name in sorted(self.mapping, key=len, reverse=True):
            out = re.sub(
                rf"\b{re.escape(name)}\b", placeholder(self.mapping[name]), out
            )
        for name in detector.find(out, locale):
            out = re.sub(
                rf"\b{re.escape(name)}\b", placeholder(self.alias_for(name)), out
            )
        return out

    def unmask(self, text: str, fallback: str = "") -> str:
        """Placeholders back to names. Anything unresolvable becomes its role descriptor, else
        `fallback`: a raw "<| … |>" must never reach a person."""
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
    def from_dict(cls, d: dict | None) -> AliasVault:
        d = d or {}
        return cls(
            mapping=dict(d.get("mapping") or {}),
            roles=dict(d.get("roles") or {}),
            prefix=d.get("prefix") or "Person",
        )


# --------------------------------------------------------------------------- channel helpers
def scrub_inbound(
    vault: AliasVault, text: str, detector: PersonDetector, locale: str = "en"
) -> tuple[str, dict[str, int]]:
    """Tier 1 then Tier 2, before the text is stored or sent anywhere."""
    clean, found = redact(text or "")
    return vault.mask(clean, detector, locale), found


def restore_outbound(
    vault: AliasVault, text: str, fallback: str = "them", redacted: str = ""
) -> str:
    """Placeholders back to names as the last step before a person reads the text.

    `redacted` (e.g. "[{} removed]") shows a Tier-1 redaction as a visible marker, for when a
    person reads back their own words; otherwise it becomes `fallback` like any unknown alias.
    """
    out = text or ""
    if redacted:
        out = PLACEHOLDER_RE.sub(
            lambda m: (
                redacted.format(m.group(1))
                if m.group(1) in TIER1_LABELS
                else m.group(0)
            ),
            out,
        )
    out = vault.unmask(out, fallback=fallback)
    if has_placeholder(out):
        out = PLACEHOLDER_RE.sub(fallback, out)
    return out


def has_placeholder(text: str) -> bool:
    """True if an unresolved placeholder survives; must be False before every send."""
    return bool(PLACEHOLDER_RE.search(text or ""))
