"""
MEET_RUDI platform — the ConversationStore seam (CLAUDE.md §2 `MemoryStore`, conventions §5).

Memory is pluggable per use case. This seam is the part the turn engine needs: load a
conversation's state, alias vault and per-conversation settings, and save the state after a turn.
Implementations:

  - InMemoryConversationStore (here): tests and local runs.
  - LegacyS3ConversationStore (legacy_s3.py): CM's `conversations/{uid}/meta.json`, so the
    platform engine can run CM before the platform database is live.
  - PlanetScale (Vitess): the record, once live (USER-TODO #3), through the tenant guard
    (rudi_platform.db.TenantBound, decision #47).

Keys are opaque to the engine. For CM's legacy store a key is the legacy `wa_…` pseudonym; on the
platform it will be the `cnv_` id.
"""

from __future__ import annotations

import copy
import datetime
from dataclasses import dataclass, field
from typing import Protocol

from ..privacy.deid import AliasVault
from .state import ConversationState


@dataclass
class StoredConversation:
    key: str
    state: ConversationState
    # AliasVault.to_dict() of the current session.
    vault: dict = field(default_factory=dict)
    locale: str = "en"  # last language the person used
    timezone: str = ""
    # Persona key the end-user is pinned to ("" = the clone's default).
    persona: str = ""
    status: str = "active"  # active | frozen | archived | blocked
    window_open_until: str = ""  # end of the 24h free-form window (UTC ISO)

    def in_window(self, at: datetime.datetime) -> bool:
        if not self.window_open_until:
            return False
        until = datetime.datetime.fromisoformat(self.window_open_until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=datetime.UTC)
        return at < until

    def session_vault(self, at: datetime.datetime) -> tuple[AliasVault, bool]:
        """(vault, is_new_session). The 24h window is the de-identification session: once it has
        closed, the alias map that could re-identify its messages is dropped."""
        if not self.in_window(at):
            return AliasVault(), True
        return AliasVault.from_dict(self.vault or None), False


class ConversationStore(Protocol):
    def load(self, key: str) -> StoredConversation | None: ...

    def save(
        self,
        key: str,
        state: ConversationState,
        *,
        vault: AliasVault,
        locale: str | None = None,
    ) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._rows: dict[str, StoredConversation] = {}

    def put(self, conv: StoredConversation) -> None:
        self._rows[conv.key] = copy.deepcopy(conv)

    def load(self, key: str) -> StoredConversation | None:
        row = self._rows.get(key)
        return copy.deepcopy(row) if row else None

    def save(
        self,
        key: str,
        state: ConversationState,
        *,
        vault: AliasVault,
        locale: str | None = None,
    ) -> None:
        row = self._rows.get(key) or StoredConversation(key, ConversationState())
        row.state = state.copy()
        row.vault = copy.deepcopy(vault.to_dict())
        if locale:
            row.locale = locale
        self._rows[key] = row
