"""
The skill envelope — one request and result shape for real-time, turn and background skills.

What this guards: a payload that survives json.dumps but loses a field on the way back, ids of
the wrong category slipping through (an end-user id where a tenant id belongs), and a continued
run that forgot its job.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import replace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.ids import Cat, new_id, pseudonym_id
from rudi_platform.skills.contract import (
    Continuation,
    MemoryWrite,
    SkillRef,
    SkillRequest,
    SkillResult,
    Usage,
    merge_context,
)

TENANT = new_id(Cat.TENANT)
CLONE = new_id(Cat.CLONE)
USER = pseudonym_id(b"tenant-salt-0123456789", "whatsapp", "+32470000000", scope=CLONE)


def request(**overrides):
    base = SkillRequest(
        skill=SkillRef.parse("knowledge-retrieval@1.4.2"),
        tenant=TENANT,
        channel="whatsapp",
        input={"query": "walking after dinner"},
        settings={"top_k": 4},
        clone=CLONE,
        clone_version=new_id(Cat.CLONE_VERSION),
        cell="pool",
        end_user=USER,
        conversation=new_id(Cat.CONVERSATION),
        turn=new_id(Cat.TURN),
        deadline_ms=1800,
    )
    return replace(base, **overrides)


class Refs(unittest.TestCase):
    def test_parse_and_print(self):
        ref = SkillRef.parse("goal-setting@2.10.0")
        self.assertEqual(ref.slug, "goal-setting")
        self.assertEqual(ref.major, 2)
        self.assertEqual(str(ref), "goal-setting@2.10.0")

    def test_rejects_loose_versions(self):
        for bad in (
            "goal-setting",
            "goal-setting@2",
            "goal-setting@2.1",
            "Goal@1.0.0",
            "goal_setting@1.0.0",
        ):
            with self.assertRaises(ValueError):
                SkillRef.parse(bad)


class Requests(unittest.TestCase):
    def test_payload_round_trip(self):
        req = request(context={"lang": "nl", "facts": ["has a dog"]})
        payload = json.loads(json.dumps(req.to_payload()))
        self.assertEqual(SkillRequest.from_payload(payload), req)

    def test_context_is_optional(self):
        req = request()
        self.assertIsNone(req.context)
        payload = req.to_payload()
        self.assertIsNone(payload["context"])
        self.assertIsNone(SkillRequest.from_payload(payload).context)

    def test_minimal_background_request(self):
        req = SkillRequest(
            skill=SkillRef.parse("knowledge-ingest@1.0.0"),
            tenant=TENANT,
            channel="system",
        )
        self.assertEqual(SkillRequest.from_payload(req.to_payload()), req)

    def test_rejects_wrong_ids_and_values(self):
        for bad in (
            {"tenant": CLONE},
            {"end_user": new_id(Cat.ACCOUNT)},
            {"channel": "fax"},
            {"cell": "shared"},
            {"deadline_ms": 0},
            {"hop": 1},  # continued run without a job
            {"context": {"when": object()}},  # not JSON
            {"input": ["not", "a", "dict"]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                request(**bad).validate()

    def test_with_context_merges(self):
        req = request(context={"a": 1, "b": 2}).with_context({"b": None, "c": 3})
        self.assertEqual(req.context, {"a": 1, "c": 3})


class MergeContext(unittest.TestCase):
    def test_rules(self):
        self.assertIsNone(merge_context(None, None))
        self.assertIsNone(merge_context(None, {"gone": None}))
        self.assertEqual(merge_context(None, {"x": 1}), {"x": 1})
        self.assertEqual(merge_context({"x": 1}, {}), {"x": 1})
        self.assertEqual(merge_context({"x": 1}, {"x": None}), {})


class Results(unittest.TestCase):
    def test_payload_round_trip(self):
        result = SkillResult(
            output={"passages": ["…"]},
            signals={"goal_status": "accepted"},
            context_patch={"retrieved": 3},
            memory_writes=(MemoryWrite("goal", "walk 20 minutes"),),
            usage=(Usage("gpt-oss-120b", 812, 64, 0.0004),),
            continuation=Continuation(
                new_id(Cat.CHECKPOINT), new_id(Cat.JOB), 2, {"page": 3}
            ),
        )
        payload = json.loads(json.dumps(result.to_payload()))
        self.assertEqual(SkillResult.from_payload(payload), result)

    def test_rejects_bad_continuation(self):
        payload = SkillResult().to_payload()
        payload["continuation"] = {
            "checkpoint": "chk_nope",
            "job": new_id(Cat.JOB),
            "hop": 1,
        }
        with self.assertRaises(ValueError):
            SkillResult.from_payload(payload)


if __name__ == "__main__":
    unittest.main()
