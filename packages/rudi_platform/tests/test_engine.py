"""
The Turn Engine, the arena door, CM's clone and the skill packages — what parity cannot show.

test_engine_parity.py proves CM v1 equals the live engine. This file covers the platform-only
behaviour: inbound gates, sessions, clone overrides, loading failures, the hook and memory
contract, and the door's rules (targets, the arena's cascade, errors).

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

import rudi_skills
from rudi_platform.engine import (
    CloneLoadError,
    DictAssetStore,
    EngineError,
    SkillLoadError,
    SkillRuntime,
    TurnEngine,
    load_clone,
    profile,
)
from rudi_platform.engine.door import Door, DoorDeps
from rudi_platform.gateway import (
    ModelEndpoint,
    ModelGateway,
    RateLimitError,
    StaticSecrets,
)
from rudi_platform.memory import ConversationState, to_flat
from rudi_platform.privacy.deid import AliasVault
from rudi_platform.registry import PLATFORM, InMemoryRegistryStore, TenantContext
from rudi_platform.registry.publishing import approve_live, publish_to_staging
from rudi_platform.skills.contract import MemoryWrite, SkillResult
from rudi_platform.skills.packs import PACKS
from rudi_skills.check_in_promise.skill import Skill as CheckInPromise
from rudi_tenants import cm

NOW = datetime.datetime(2026, 9, 14, 7, 30, tzinfo=datetime.UTC)
ASSETS = {
    cm.ASSETS["about"]: "About Rudi.",
    cm.ASSETS["health_guidance"]: "Walk after meals.",
}
ARENA_SECRET = "meetrudi/arena/rudi-groq"
ENDPOINT = ModelEndpoint(
    name="fake", url="https://fake.example/v1/chat/completions", model="fake-model",
    secret=ARENA_SECRET, region="eu", dpa=True,
)  # fmt: skip


class Transport:
    def __init__(self):
        self.replies: list = []
        self.calls: list = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append(payload["messages"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {
            "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }


def env(reply, **signals):
    return json.dumps({"reply": reply, "signals": signals})


class World:
    """CM seeded into a fresh registry, with a scripted model."""

    def __init__(self, runtime=None, assets=None):
        self.store = InMemoryRegistryStore()
        self.seeded = cm.seed(self.store, rudi_skills.manifests())
        self.runtime = runtime or rudi_skills.runtime()
        self.assets = DictAssetStore(ASSETS if assets is None else assets)
        self.transport = Transport()
        self.gateway = ModelGateway(
            self.transport, StaticSecrets({ARENA_SECRET: "k"}), sleep=lambda s: None
        )

    def clone(self, version_id=None):
        return load_clone(
            self.store,
            PLATFORM,
            version_id or self.seeded.version_id,
            self.runtime,
            self.assets,
        )

    def engine(self, version_id=None):
        return TurnEngine(self.clone(version_id), self.gateway, [ENDPOINT])

    def new_version(self, **changes):
        """A CM clone version 2 with `changes` to the composition/settings, published live."""
        ctx = TenantContext(self.seeded.tenant_id)
        draft = self.store.create_draft(
            ctx, self.seeded.clone_id, self.seeded.version_id
        )
        self.store.update_draft(ctx, draft.id, **changes)
        publish_to_staging(self.store, ctx, draft.id)
        approve_live(self.store, PLATFORM, draft.id)
        return draft.id


def in_stage(stage, **memory):
    return ConversationState(stage=stage, session=1, memory=memory)


class Engine(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_open_greets_without_a_model_call(self):
        o = self.w.engine().open(now=NOW)
        self.assertTrue(o.greeted and o.new_contact)
        self.assertIn("I am Rudi", o.reply)
        self.assertEqual((o.state.stage, o.state.session), ("learn", 1))
        self.assertEqual(self.w.transport.calls, [])

    def test_the_objection_gate_stops_before_the_model(self):
        state = in_stage("goal", clarifiers_used=1)
        o = self.w.engine().turn(state, "Wrong number, who is this?", vault=AliasVault(),
                                 locale="en", now=NOW)  # fmt: skip
        self.assertEqual(o.halt["kind"], "wrong_number")
        self.assertIn("stopped messaging you", o.reply)
        self.assertEqual(o.state, state)
        self.assertEqual(self.w.transport.calls, [])

    def test_a_new_session_keeps_the_goal_and_drops_session_memory(self):
        state = ConversationState(
            stage="concluded", session=3,
            memory={"goal": "Walk", "goal_domain": "fitness", "commitment": "x",
                    "commit_attempts": 7, "check_in_at": "2026-09-14T09:00:00+00:00"},
        )  # fmt: skip
        o = self.w.engine().turn(state, "hoi", vault=AliasVault(), locale="nl", now=NOW)
        self.assertIn("Fijn dat je er weer bent", o.reply)
        self.assertEqual(o.state.session, 4)
        self.assertEqual(o.state.memory, {"goal": "Walk", "goal_domain": "fitness"})

    def test_a_stage_the_clone_no_longer_has_starts_over(self):
        o = self.w.engine().turn(
            in_stage("onboarding"), "hi", vault=AliasVault(), now=NOW
        )
        self.assertTrue(o.greeted)
        self.assertEqual(o.state.stage, "learn")

    def test_a_turn_records_usage_tags(self):
        self.w.transport.replies.append(env("Hi!", want_to_try=False, lang="en"))
        o = self.w.engine().turn(in_stage("learn"), "hey", vault=AliasVault(), now=NOW)
        self.assertEqual(o.generation.usage.tokens_in, 12)
        self.assertEqual(o.generation.endpoint, "fake")
        self.assertEqual(o.lang, "en")

    def test_clone_overrides_reach_the_prompt(self):
        overrides = {
            "goal-setting": {"prompt_overrides": {
                "body": {"mode": "append", "text": {"*": "Ask about sleep first."}}}},
            "check-in-promise": {"settings": {"quiet_start": "22:00"}},
        }  # fmt: skip
        composition = copy.deepcopy(cm.COMPOSITION)
        for item in composition["skills"]:
            item.update(overrides.get(item["skill"], {}))
        version = self.w.new_version(composition=composition)
        self.w.transport.replies.append(
            env("Tell me.", goal_status="unclear", lang="en")
        )
        o = self.w.engine(version).turn(
            in_stage("goal"), "a goal", vault=AliasVault(), now=NOW
        )
        self.assertIn("Ask about sleep first.", o.system)
        self.assertIn("never a time between 22:00 and 06:30", o.system)
        replace = copy.deepcopy(composition)
        for item in replace["skills"]:
            if item["skill"] == "goal-setting":
                item["prompt_overrides"]["body"]["mode"] = "replace"
        version = self.w.new_version(composition=replace)
        self.w.transport.replies.append(
            env("Tell me.", goal_status="unclear", lang="en")
        )
        o = self.w.engine(version).turn(
            in_stage("goal"), "a goal", vault=AliasVault(), now=NOW
        )
        self.assertIn("Ask about sleep first.", o.system)
        self.assertNotIn("now in the real coaching experience", o.system)

    def test_a_skill_cannot_write_memory_it_does_not_declare(self):
        class Rogue(CheckInPromise):
            def on_signals(self, req):
                return SkillResult(memory_writes=(MemoryWrite("goal", "hijacked"),))

        skills = [s for s in rudi_skills.load_all() if s.slug != "check-in-promise"]
        rogue = Rogue(os.path.join(rudi_skills.HERE, "check_in_promise"))
        w = World(runtime=SkillRuntime([*skills, rogue]))
        w.transport.replies.append(env("Hi", want_to_try=False, lang="en"))
        with self.assertRaises(EngineError):
            w.engine().turn(in_stage("learn"), "hey", vault=AliasVault(), now=NOW)

    def test_gateway_errors_propagate(self):
        self.w.transport.replies.extend([RateLimitError("429")] * 3)
        from rudi_platform.gateway import AllRateLimited

        with self.assertRaises(AllRateLimited):
            self.w.engine().turn(in_stage("learn"), "hey", vault=AliasVault(), now=NOW)


class Loading(unittest.TestCase):
    def test_a_required_asset_must_load_an_optional_one_need_not(self):
        with self.assertRaises(CloneLoadError) as caught:
            World(assets={}).clone()
        self.assertIn("'about'", " ".join(caught.exception.problems))
        clone = World(assets={cm.ASSETS["about"]: "About."}).clone()
        self.assertEqual(clone.assets["health_guidance"], "")

    def test_a_build_without_the_pinned_code_cannot_run_the_clone(self):
        skills = [s for s in rudi_skills.load_all() if s.slug != "goal-setting"]
        with self.assertRaises(CloneLoadError) as caught:
            World(runtime=SkillRuntime(skills)).clone()
        self.assertIn("goal-setting@1.0.0", " ".join(caught.exception.problems))

    def test_code_and_manifest_must_agree_on_hooks(self):
        class Extra(CheckInPromise):
            def on_reply(self, req):
                return SkillResult()

        with self.assertRaises(SkillLoadError):
            SkillRuntime([Extra(os.path.join(rudi_skills.HERE, "check_in_promise"))])


class DoorSetup:
    """CM behind the arena door, with the arena's own cascade."""

    def setUp(self):
        self.w = World()
        self.door = Door(
            DoorDeps(
                registry=self.w.store,
                runtime=self.w.runtime,
                assets=self.w.assets,
                gateway=self.w.gateway,
                clones={"cm/rudi": self.w.seeded.clone_id},
                allowed_hosts=frozenset({"fake.example"}),
                release="test",
            )
        )
        self.cascade = [{"name": "fake", "kind": "openai_compatible", "model": "fake-model",
                         "endpoint": "https://fake.example/v1/chat/completions",
                         "secret": ARENA_SECRET}]  # fmt: skip


class Door_(DoorSetup, unittest.TestCase):
    def test_open_turn_and_state_round_trip(self):
        opened = self.door.handle({"op": "open", "target": "cm/rudi@live"})
        self.assertTrue(opened["ok"])
        self.assertEqual(opened["state"]["phase"], "learn")
        self.assertEqual(opened["engine"]["pins"]["goal-setting"], "goal-setting@1.0.0")
        self.w.transport.replies.append(env("Let's go!", want_to_try=True, lang="en"))
        turn = self.door.handle({
            "op": "turn", "target": "cm/rudi@1", "text": "coach me, I went running with Peter",
            "state": opened["state"], "vault": opened["vault"], "cascade": self.cascade,
            "now": "2026-09-14T07:31:00Z",
        })  # fmt: skip
        self.assertTrue(turn["ok"], turn)
        self.assertEqual(
            (turn["info"]["phase_before"], turn["info"]["phase"]), ("learn", "goal")
        )
        self.assertEqual(turn["usage"]["tokens_in"], 12)
        self.assertEqual(turn["state"]["history"], [])
        self.assertEqual(turn["vault"]["mapping"], {"Peter": "Person_A"})

    def test_a_halt_is_reported(self):
        out = self.door.handle({"op": "turn", "text": "STOP", "state": {"phase": "learn"},
                                "cascade": self.cascade})  # fmt: skip
        self.assertTrue(out["ok"])
        self.assertEqual(out["info"]["halted"]["kind"], "unsubscribe")

    def test_targets(self):
        self.assertEqual(self.door.handle({"op": "info"})["personalities"],
                         ["seed-rudi-v2", "seed-rudi-v3"])  # fmt: skip
        for bad in ("cm/other@live", "cm/rudi@9", "nonsense"):
            self.assertEqual(
                self.door.handle({"op": "info", "target": bad})["error"], "bad_target"
            )

    def test_the_arena_cascade_rules(self):
        base = {"op": "turn", "text": "hi", "state": {"phase": "learn"}}
        cases = [
            None,
            [],
            [{**self.cascade[0], "endpoint": "https://elsewhere.example/v1"}],
            [{**self.cascade[0], "secret": "meetrudi-groq-firstkey"}],
            [{**self.cascade[0], "kind": "bedrock"}],
            [{**self.cascade[0], "enabled": False}],
        ]
        for cascade in cases:
            out = self.door.handle({**base, "cascade": cascade})
            self.assertEqual(out["error"], "bad_cascade", cascade)
        self.assertEqual(self.w.transport.calls, [])

    def test_errors_never_leak_a_stack_trace(self):
        self.w.transport.replies.extend([RateLimitError("429")] * 3)
        out = self.door.handle({"op": "turn", "text": "hi", "state": {"phase": "learn"},
                                "cascade": self.cascade})  # fmt: skip
        self.assertEqual(out["error"], "rate_limited")
        self.assertEqual(self.door.handle({"op": "nope"})["error"], "bad_request")
        self.assertEqual(self.door.handle("garbage")["error"], "bad_request")
        out = self.door.handle({"op": "turn", "channel": "widget", "text": "hi"})
        self.assertIn("does not serve", out["detail"])
        self.assertEqual(
            self.door.handle({"op": "turn", "text": " "})["error"], "bad_request"
        )
        scan = self.door.handle({"op": "scan", "texts": ["mail me: a.b@example.org"]})
        self.assertEqual(scan["found"], [{"email-address": 1}])


CALL = {
    "user_name": "Jan",
    "topic": "walk more",
    "language": "nl-BE",
    "timezone": "Europe/Brussels",
    "call_goal": "SET_NEARTERM_GOAL",
    "start_phase": "goal",
}


class VoiceDoor(DoorSetup, unittest.TestCase):
    def test_a_call_through_the_door(self):
        self.w.transport.replies.append(env("Hoi <| Person_A |>!"))
        opened = self.door.handle(
            {"op": "open", "channel": "voice", "call": CALL, "cascade": self.cascade}
        )
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["reply"], "Hoi Jan!")
        self.assertEqual(opened["state"]["phase"], "goal")
        self.assertFalse(opened["state"]["disclosed"])
        self.assertNotIn(
            "check_in_at", opened["state"]
        )  # the call's state, not WhatsApp's
        self.w.transport.replies.append(
            env("Ik ben Rudi, een AI.", goal_status="unclear")
        )
        turn = self.door.handle({
            "op": "turn", "channel": "voice", "call": CALL, "cascade": self.cascade,
            "text": "Hallo?", "state": opened["state"], "vault": opened["vault"],
            "elapsed_s": 700,
        })  # fmt: skip
        self.assertTrue(turn["ok"], turn)
        self.assertTrue(
            turn["info"]["ended"]
        )  # 12 minutes are up: say this, then hang up
        self.assertTrue(turn["state"]["disclosed"])
        system = self.w.transport.calls[-1][0]["content"]
        self.assertIn("# Speaking, not writing", system)
        self.assertNotIn(
            "Your personality profile", system
        )  # no persona block on calls
        after = self.door.handle({
            "op": "turn", "channel": "voice", "call": CALL, "cascade": self.cascade,
            "text": "Hallo?", "state": turn["state"], "vault": turn["vault"],
        })  # fmt: skip
        self.assertEqual((after["reply"], after["info"]["ended"]), ("", True))
        self.assertEqual(
            len(self.w.transport.calls), 2
        )  # nothing said once it has ended

    def test_a_call_needs_the_arena_cascade_to_open(self):
        out = self.door.handle({"op": "open", "channel": "voice", "call": CALL})
        self.assertEqual(out["error"], "bad_cascade")

    def test_who_is_this_does_not_freeze_a_call(self):
        self.w.transport.replies.append(env("Hoi!"))
        opened = self.door.handle(
            {"op": "open", "channel": "voice", "call": CALL, "cascade": self.cascade}
        )
        self.w.transport.replies.append(env("Met Rudi, een AI.", goal_status="unclear"))
        turn = self.door.handle({
            "op": "turn", "channel": "voice", "call": CALL, "cascade": self.cascade,
            "text": "Who is this?", "state": opened["state"], "vault": opened["vault"],
        })  # fmt: skip
        self.assertNotIn("halted", turn["info"])
        self.assertEqual(turn["reply"], "Met Rudi, een AI.")


class Assets(unittest.TestCase):
    def test_a_channel_version_replaces_the_base_where_it_has_text(self):
        w = World(
            assets={
                **ASSETS,
                cm.ASSETS["health_guidance@voice"]: "Call guidance.",
            }
        )
        clone = w.clone()
        self.assertEqual(clone.assets_for("voice")["health_guidance"], "Call guidance.")
        self.assertEqual(
            clone.assets_for("whatsapp")["health_guidance"], "Walk after meals."
        )
        self.assertNotIn("health_guidance@voice", clone.assets_for("voice"))
        bare = World().clone()  # the call's own version missing: calls use the base one
        self.assertEqual(
            bare.assets_for("voice")["health_guidance"], "Walk after meals."
        )


class Speech(unittest.TestCase):
    def test_an_alias_without_brackets_is_restored_not_spoken(self):
        """Regression (2026-11-04): a fallback model wrote "Hi Person_A"; the "_" became a space
        and the caller heard "Person A". Same rule as services/call brain._speakable."""
        from rudi_platform.engine.channels import speakable
        from rudi_platform.privacy.deid import AliasVault, restore_outbound

        vault = AliasVault()
        vault.alias_for("Filip")
        for bare in ("Hi Person_A, ready?", "Hi Person A, ready?"):
            self.assertEqual(
                restore_outbound(vault, speakable(bare)), "Hi Filip, ready?"
            )
        self.assertEqual(restore_outbound(vault, speakable("Hi Person_B!")), "Hi them!")
        for prose in ("A Person Andrew knows", "Personal best", "the Person AI said"):
            self.assertEqual(speakable(prose), prose)

    def test_a_call_reply_has_room_to_think(self):
        """gpt-oss's thinking counts against the cap; at 250 every call turn came back empty."""
        self.assertGreaterEqual(profile("voice").max_tokens, 1024)


class CM(unittest.TestCase):
    def test_clone_version_1(self):
        w = World()
        self.assertEqual(
            set(w.seeded.review_reasons),
            {
                (
                    "baseline skill crisis-signposting changed: needs a contract addendum, "
                    "review and an arena gate"
                ),
                (
                    "baseline skill acceptable-use removed: needs a contract addendum, review "
                    "and an arena gate"
                ),
                (
                    "baseline skill objection-gate changed: needs a contract addendum, review "
                    "and an arena gate"
                ),
                "processes health data: consent capture must be configured for this clone",
            },
        )
        clone = w.clone()
        self.assertEqual(clone.composed.warnings, ())
        self.assertEqual(clone.default_persona, "seed-rudi-v2")
        slugs = {s.slug for s in clone.skills}
        self.assertEqual(
            slugs, {s for pack in PACKS.values() for s in pack} - cm.LEFT_OUT
        )
        releases = [
            e for e in w.store.audit_events(PLATFORM) if e.action == "skill.release"
        ]
        self.assertEqual(len(releases), len(rudi_skills.load_all()))
        self.assertTrue(all(e.decision["gate"] == "legacy-parity" for e in releases))

    def test_flat_state_is_cms_ai_state(self):
        clone = World().clone()
        flat = to_flat(
            ConversationState(stage="learn", session=1), clone.memory_schema("whatsapp")
        )
        self.assertEqual(
            set(flat),
            {"phase", "session_id", "history", "clarifiers_used", "commit_attempts",
             "reject_count", "goal", "goal_domain", "commitment", "commitment_agreed_at",
             "check_in_at"},
        )  # fmt: skip
        self.assertEqual(clone.composed.per_channel.keys(), {"whatsapp", "voice"})
        on_calls = set(clone.composed.per_channel["voice"])
        self.assertNotIn(
            "persona-ocean", on_calls
        )  # calls carry no persona block today
        self.assertNotIn(
            "objection-gate", on_calls
        )  # "who is this?" is normal on a call
        voice = to_flat(
            ConversationState(stage="goal", session=1), clone.memory_schema("voice")
        )
        self.assertFalse(voice["disclosed"])
        self.assertNotIn("check_in_at", voice)  # the call reports minutes, as today


class Packages(unittest.TestCase):
    def test_every_pack_skill_has_a_package_and_vice_versa(self):
        built = {s.slug for s in rudi_skills.load_all()}
        packed = {s for pack in PACKS.values() for s in pack}
        self.assertEqual(built, packed)

    def test_every_skill_is_version_1_and_real_time(self):
        for skill in rudi_skills.load_all():
            self.assertEqual(skill.manifest.version, (1, 0, 0), skill.slug)


if __name__ == "__main__":
    unittest.main()
