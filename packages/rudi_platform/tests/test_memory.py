"""
Conversation memory: the state's flat form (CM's `ai_state`), the in-memory store, and the
adapter onto CM's legacy S3 layout.

What this guards: a legacy state surviving a round trip through the platform unchanged (so the
two engines can hand a conversation back and forth), and the adapter writing nothing but its own
three fields of meta.json — never the counters, schedule or freeze that the legacy processor owns.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import datetime
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.memory import (
    ConversationState,
    InMemoryConversationStore,
    LegacyS3ConversationStore,
    StoredConversation,
    from_flat,
    to_flat,
)
from rudi_platform.privacy.deid import AliasVault

SCHEMA = {
    "goal": "str",
    "goal_domain": "str",
    "clarifiers_used": "int",
    "reject_count": "int",
    "commitment": "str",
    "commitment_agreed_at": "datetime",
    "commit_attempts": "int",
    "check_in_at": "datetime",
}
LEGACY = {
    "phase": "commit",
    "session_id": 2,
    "history": [{"role": "user", "content": "hi", "at": "2026-09-14T07:30:00+00:00"}],
    "clarifiers_used": 2,
    "commit_attempts": 1,
    "reject_count": 0,
    "goal": "Walk daily",
    "goal_domain": "fitness",
    "commitment": None,
    "commitment_agreed_at": None,
    "check_in_at": "2026-09-15T07:30:00+00:00",
}
NOW = datetime.datetime(2026, 9, 14, 8, 0, tzinfo=datetime.UTC)


class Flat(unittest.TestCase):
    def test_legacy_state_round_trips(self):
        state = from_flat(LEGACY, SCHEMA)
        self.assertEqual(state.stage, "commit")
        self.assertEqual(state.session, 2)
        self.assertEqual(state.memory["goal"], "Walk daily")
        self.assertEqual(to_flat(state, SCHEMA), LEGACY)

    def test_never_spoken_is_empty(self):
        self.assertTrue(from_flat({}, SCHEMA).is_new)
        self.assertTrue(from_flat(None, SCHEMA).is_new)
        self.assertEqual(to_flat(ConversationState(), SCHEMA), {})

    def test_missing_fields_get_legacy_defaults(self):
        flat = to_flat(ConversationState(stage="learn", session=1), SCHEMA)
        self.assertEqual(flat["clarifiers_used"], 0)
        self.assertIsNone(flat["goal"])
        self.assertEqual(set(flat), {"phase", "session_id", "history", *SCHEMA})

    def test_unknown_keys_survive(self):
        flat = {**LEGACY, "experiment": {"arm": "b"}}
        self.assertEqual(to_flat(from_flat(flat, SCHEMA), SCHEMA), flat)
        state = ConversationState(stage="learn", session=1, memory={"retired_field": 3})
        self.assertEqual(to_flat(state, SCHEMA)["retired_field"], 3)

    def test_a_state_without_phase_is_a_concluded_session(self):
        self.assertEqual(
            from_flat({"session_id": 4, "goal": "x"}, SCHEMA).stage, "concluded"
        )

    def test_copies_do_not_share_history(self):
        state = from_flat(LEGACY, SCHEMA)
        flat = to_flat(state, SCHEMA)
        flat["history"].append({"role": "user", "content": "x"})
        self.assertEqual(len(state.history), 1)


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts = 0
        self.fail: Exception | None = None

    def get_object(self, Bucket, Key):
        if self.fail:
            raise self.fail
        if Key not in self.objects:
            raise KeyError(f"NoSuchKey: {Key}")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.puts += 1
        self.objects[Key] = Body


def meta(**kw):
    base = {
        "user_id": "wa_abc",
        "phone": "+32470000000",
        "locale": "nl",
        "timezone": "Europe/Brussels",
        "persona": "seed-rudi-v3",
        "status": "active",
        "window_open_until": "2026-09-15T07:00:00+00:00",
        "unread_count": 2,
        "next_proactive_at": "2026-09-14T19:00:00+00:00",
        "commitment_at": "2026-09-14T12:00:00+00:00",
        "msg_total": 9,
        "a_field_from_the_future": [1, 2],
        "ai_state": LEGACY,
        "alias_vault": {
            "mapping": {"Peter": "Person_A"},
            "roles": {},
            "prefix": "Person",
        },
    }
    base.update(kw)
    return base


class LegacyStore(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.s3.objects["conversations/wa_abc/meta.json"] = json.dumps(meta()).encode()
        self.store = LegacyS3ConversationStore(self.s3, "bucket", SCHEMA)

    def test_load(self):
        conv = self.store.load("wa_abc")
        self.assertEqual(conv.state.stage, "commit")
        self.assertEqual(conv.locale, "nl")
        self.assertEqual(conv.timezone, "Europe/Brussels")
        self.assertEqual(conv.persona, "seed-rudi-v3")
        self.assertTrue(conv.in_window(NOW))
        vault, new = conv.session_vault(NOW)
        self.assertFalse(new)
        self.assertEqual(vault.mapping, {"Peter": "Person_A"})
        self.assertIsNone(self.store.load("wa_nobody"))

    def test_save_touches_only_its_fields(self):
        conv = self.store.load("wa_abc")
        conv.state.stage = "committed"
        conv.state.memory["commitment"] = "a walk"
        vault = AliasVault.from_dict(conv.vault)
        vault.alias_for("Sophie")
        self.store.save("wa_abc", conv.state, vault=vault, locale="fr")
        after = json.loads(self.s3.objects["conversations/wa_abc/meta.json"])
        before = meta()
        for key in ("ai_state", "alias_vault", "locale"):
            before.pop(key)
            after_value = after.pop(key)
            self.assertIsNotNone(after_value)
        self.assertEqual(
            after, before
        )  # everything the legacy processor owns, untouched
        reread = self.store.load("wa_abc")
        self.assertEqual(reread.state.stage, "committed")
        self.assertEqual(reread.locale, "fr")
        self.assertIn("Sophie", reread.vault["mapping"])

    def test_save_never_creates_a_conversation(self):
        with self.assertRaises(LookupError):
            self.store.save("wa_nobody", ConversationState(), vault=AliasVault())
        self.assertEqual(self.s3.puts, 0)

    def test_keys_stay_inside_the_prefix(self):
        for bad in ("", "../x", "a/b", ".hidden"):
            with self.assertRaises(ValueError):
                self.store.load(bad)

    def test_other_read_errors_propagate(self):
        self.s3.fail = PermissionError("AccessDenied")
        with self.assertRaises(PermissionError):
            self.store.load("wa_abc")


class InMemory(unittest.TestCase):
    def test_round_trip_and_windows(self):
        store = InMemoryConversationStore()
        self.assertIsNone(store.load("k"))
        store.save(
            "k",
            ConversationState(stage="learn", session=1),
            vault=AliasVault(),
            locale="de",
        )
        conv = store.load("k")
        self.assertEqual((conv.state.stage, conv.locale), ("learn", "de"))
        self.assertFalse(conv.in_window(NOW))  # no inbound recorded: no open window
        vault, new = conv.session_vault(NOW)
        self.assertTrue(new)
        self.assertEqual(vault.mapping, {})
        store.put(
            StoredConversation(
                "w", ConversationState(), window_open_until="2026-09-14T09:00:00"
            )
        )
        self.assertTrue(store.load("w").in_window(NOW))  # naive timestamps read as UTC


if __name__ == "__main__":
    unittest.main()
