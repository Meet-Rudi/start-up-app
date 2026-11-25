"""
MEET_RUDI platform — identifiers.

Every object the platform stores carries an id of the form

    {cat}_{body}

`cat` is a three-letter category from `Cat` below. `body` is 32 characters of lowercase
Crockford base32 (digits and a-z without i, l, o, u), i.e. 160 bits. The prefix tells a reader,
a log line or an IAM condition what kind of object the id names without a lookup.

Two ways to mint one:

- `new_id(Cat.TENANT)` — random. Every id except the end-user id.
- `pseudonym_id(salt, channel, handle, scope=...)` — derived. The end-user id is an HMAC of the
  tenant's salt and a channel handle (a phone number, a token subject), so the same person on the
  same channel always maps to the same id inside one tenant, and to unrelated ids across tenants
  (decision #29, blueprint B3). It has the same shape as a random id; nothing downstream can tell.

`Cat` is the single registry of prefixes. Add a member here and a row in
docs/platform/conventions.md in the same change (a test holds the two together). Never rename
or reuse a prefix: stored ids carry it for as long as the data lives.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from enum import Enum

ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
BODY_LEN = 32
_BODY_BYTES = BODY_LEN * 5 // 8  # 20 bytes = 160 bits
ID_RE = re.compile(r"^(?P<cat>[a-z]{3})_(?P<body>[0-9a-hjkmnp-tv-z]{32})$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MIN_SALT_BYTES = 16


class Cat(str, Enum):
    """Id categories. The value is the prefix; the member name is what code uses."""

    # Tenancy and registry
    PROSPECT = "prs"
    TENANT = "ten"
    CELL = "cel"
    CLONE = "cln"
    CLONE_VERSION = "cvr"
    PERSONA = "per"
    JOURNEY = "jny"
    ENDPOINT = "enp"
    TEMPLATE = "tpl"
    LIBRARY = "lib"
    DOCUMENT = "doc"
    MODEL_POLICY = "mpl"
    MODEL_ENDPOINT = "mep"
    # Skills and their making
    SKILL = "skl"
    SKILL_VERSION = "skv"
    SKILL_PACK = "pak"
    SKILL_BRIEF = "brf"
    SCOPE_REQUEST = "scp"
    MINT_REQUEST = "mnt"
    GATE_RUN = "gat"
    # Access
    ACCOUNT = "acc"
    ROLE = "rol"
    PERMISSION = "prm"
    ROLE_ASSIGNMENT = "rga"
    SESSION = "ses"
    API_KEY = "key"
    WEBHOOK = "whk"
    INVITATION = "inv"
    # End-users and conversations
    USER = "usr"
    HANDLE = "hdl"
    IDENTITY_LINK = "lnk"
    CONSENT = "cns"
    CONVERSATION = "cnv"
    TURN = "trn"
    MESSAGE = "msg"
    SCHEDULE = "sch"
    ESCALATION = "esc"
    TAKEOVER = "tko"
    # Operations
    USAGE = "use"
    BUDGET = "bud"
    ALERT = "alr"
    AUDIT = "aud"
    ERASURE = "era"
    JOB = "job"
    CHECKPOINT = "chk"
    EVENT = "evt"
    MIGRATION = "mig"


DESCRIPTIONS: dict[Cat, str] = {
    Cat.PROSPECT: "An organisation's request to adopt Rudi, before it is a tenant",
    Cat.TENANT: "A customer organisation on the platform",
    Cat.CELL: "One deployed copy of the runtime stacks (pooled or dedicated)",
    Cat.CLONE: "A tenant's configured conversational agent",
    Cat.CLONE_VERSION: "An immutable published state of a clone (draft, staging, live)",
    Cat.PERSONA: "A face of a clone: name, avatar, OCEAN profile, voice, languages",
    Cat.JOURNEY: "A stage graph a clone version follows",
    Cat.ENDPOINT: "A channel endpoint: WhatsApp sender, phone number, widget, API entry",
    Cat.TEMPLATE: "A pre-approved out-of-window message template",
    Cat.LIBRARY: "A knowledge library (tenant upload or curated)",
    Cat.DOCUMENT: "One document inside a knowledge library",
    Cat.MODEL_POLICY: "Which models a tenant or clone may use",
    Cat.MODEL_ENDPOINT: "One model endpoint in a cascade, with region and DPA tags",
    Cat.SKILL: "A skill, across all its versions",
    Cat.SKILL_VERSION: "One released version of a skill",
    Cat.SKILL_PACK: "A named bundle of skills with defaults",
    Cat.SKILL_BRIEF: "A training brief (rudi-skill-brief@1) for a skill version",
    Cat.SCOPE_REQUEST: "A tenant's desired clone capabilities, matched against the skill inventory",
    Cat.MINT_REQUEST: "Development of a new skill, from scope gap to release",
    Cat.GATE_RUN: "One arena gate run for a skill version or clone version",
    Cat.ACCOUNT: "A console user account",
    Cat.ROLE: "A role: a named set of permissions, seeded or created by a super-admin",
    Cat.PERMISSION: "One permission in the access catalogue",
    Cat.ROLE_ASSIGNMENT: "A role given to an account, platform-wide or in one tenant",
    Cat.SESSION: "A console sign-in session",
    Cat.API_KEY: "An API key for one clone and stage",
    Cat.WEBHOOK: "A tenant webhook endpoint with its signing secret",
    Cat.INVITATION: "An invitation to join a tenant's console",
    Cat.USER: "An end-user; derived from the tenant salt and a channel handle",
    Cat.HANDLE: "One channel handle of an end-user (phone, token subject, login)",
    Cat.IDENTITY_LINK: "A tenant-approved link between end-user ids",
    Cat.CONSENT: "A consent record for one end-user and clone",
    Cat.CONVERSATION: "An end-user's relationship with one clone",
    Cat.TURN: "One inbound-to-reply cycle, or one proactive send",
    Cat.MESSAGE: "One stored message",
    Cat.SCHEDULE: "A due item: check-in, reach-out or retention job",
    Cat.ESCALATION: "A red-flag event routed to a tenant contact",
    Cat.TAKEOVER: "An operator taking over a conversation",
    Cat.USAGE: "One metered model or skill call",
    Cat.BUDGET: "A spend budget for a tenant, clone or end-user",
    Cat.ALERT: "A raised alert (spend, errors, safety)",
    Cat.AUDIT: "One audit-log entry",
    Cat.ERASURE: "An erasure request and its receipt",
    Cat.JOB: "A chain of Lambda runs doing one piece of long work",
    Cat.CHECKPOINT: "A saved state one Lambda run hands to the next",
    Cat.EVENT: "One event on the event bus",
    Cat.MIGRATION: "One data-migration run",
}


def _encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    chars = []
    for _ in range(len(data) * 8 // 5):
        chars.append(ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(chars))


def new_id(cat: Cat) -> str:
    """A fresh random id in category `cat`."""
    if cat is Cat.USER:
        raise ValueError("end-user ids are derived: use pseudonym_id()")
    return f"{cat.value}_{_encode(secrets.token_bytes(_BODY_BYTES))}"


def pseudonym_id(salt: bytes, channel: str, handle: str, scope: str = "") -> str:
    """The end-user id for `handle` on `channel`, under one tenant's `salt`.

    `scope` is a clone id when identities are kept per clone (the default), or "" when the tenant
    links them tenant-wide. `handle` must already be normalised by the caller (E.164 for phone
    numbers, the token's `sub` as issued), because any difference in spelling is a different
    person here.
    """
    if len(salt) < MIN_SALT_BYTES:
        raise ValueError(f"tenant salt must be at least {MIN_SALT_BYTES} bytes")
    if not _SLUG_RE.match(channel):
        raise ValueError(f"channel must be a lowercase slug, got {channel!r}")
    if not handle:
        raise ValueError("handle is empty")
    if scope:
        require(scope, Cat.CLONE)
    message = f"{scope}|{channel}|{handle}"
    digest = hmac.new(salt, message.encode("utf-8"), hashlib.sha256).digest()
    return f"{Cat.USER.value}_{_encode(digest[:_BODY_BYTES])}"


def category_of(value: str) -> Cat | None:
    """The category of a well-formed id, or None if `value` is not one."""
    m = ID_RE.match(value) if isinstance(value, str) else None
    if not m:
        return None
    try:
        return Cat(m.group("cat"))
    except ValueError:
        return None


def is_id(value: object, cat: Cat | None = None) -> bool:
    """True if `value` is a well-formed id (of category `cat`, when given)."""
    found = category_of(value) if isinstance(value, str) else None
    return found is not None and (cat is None or found is cat)


def require(value: object, cat: Cat) -> str:
    """Return `value` if it is an id of category `cat`; raise ValueError otherwise."""
    if not is_id(value, cat):
        raise ValueError(
            f"expected a {cat.name.lower()} id ({cat.value}_…), got {value!r}"
        )
    return value  # type: ignore[return-value]
