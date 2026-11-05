"""
MEET_RUDI platform — CM's legacy S3 conversation layout behind the ConversationStore seam
(blueprint §7.2: "the new engine can run CM on it through an adapter").

Layout (services/whatsapp/src/store.py, unchanged):

    conversations/{uid}/meta.json      contact record; `ai_state` is the engine state (flat form),
                                       `alias_vault` the session's name map, plus `locale`,
                                       `timezone`, `persona`, `status`, `window_open_until`
    conversations/{uid}/messages/...   the transcript (written by the WhatsApp processor)
    conversations/{uid}/profile.json   goal and most-recent development (processor)

This adapter reads a conversation and, after a turn, writes back exactly three fields of
`meta.json`: `ai_state`, `alias_vault` and `locale`. Every other field — counters, scheduling,
freeze state, fields this code does not know — is carried through untouched, because the legacy
processor and runner own them until CM is cut over. It never creates a conversation: in the
legacy layout only an inbound message does that.

Like the legacy store it is read-modify-write without a conditional put; the WhatsApp queue is
FIFO per user, so two turns of one conversation never run at once.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

from ..privacy.deid import AliasVault
from .state import ConversationState, from_flat, to_flat
from .store import StoredConversation

PREFIX = "conversations"


class S3Like(Protocol):
    def get_object(self, **kw: Any) -> Any: ...
    def put_object(self, **kw: Any) -> Any: ...


def _missing(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}"
    return "NoSuchKey" in text or "404" in text or "Not Found" in text


class LegacyS3ConversationStore:
    def __init__(
        self,
        s3: S3Like,
        bucket: str,
        schema: Mapping[str, str],
        prefix: str = PREFIX,
    ) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._schema = dict(schema)
        self._prefix = prefix.rstrip("/")

    def _key(self, uid: str) -> str:
        if not uid or "/" in uid or uid.startswith("."):
            raise ValueError("a legacy conversation key is one path segment")
        return f"{self._prefix}/{uid}/meta.json"

    def _read(self, uid: str) -> dict[str, Any] | None:
        try:
            body = self._s3.get_object(Bucket=self._bucket, Key=self._key(uid))[
                "Body"
            ].read()
        except Exception as e:
            if _missing(e):
                return None
            raise
        data = json.loads(body)
        return data if isinstance(data, dict) else None

    def load(self, key: str) -> StoredConversation | None:
        meta = self._read(key)
        if meta is None:
            return None
        return StoredConversation(
            key=key,
            state=from_flat(meta.get("ai_state") or {}, self._schema),
            vault=dict(meta.get("alias_vault") or {}),
            locale=str(meta.get("locale") or "en"),
            timezone=str(meta.get("timezone") or ""),
            persona=str(meta.get("persona") or ""),
            status=str(meta.get("status") or "active"),
            window_open_until=str(meta.get("window_open_until") or ""),
        )

    def save(
        self,
        key: str,
        state: ConversationState,
        *,
        vault: AliasVault,
        locale: str | None = None,
    ) -> None:
        meta = self._read(key)
        if meta is None:
            raise LookupError(
                "no legacy conversation to update; an inbound message creates it"
            )
        meta["ai_state"] = to_flat(state, self._schema)
        meta["alias_vault"] = vault.to_dict()
        if locale:
            meta["locale"] = locale
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key(key),
            Body=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
