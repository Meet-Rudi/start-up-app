"""
The 13-minute rule and hand-over — no Lambda run works past 13 minutes.

What this guards: a long job overrunning into AWS's 15-minute kill, a successor started before
its checkpoint exists, and a job that keeps handing over forever.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.ids import Cat, new_id
from rudi_platform.runtime.budget import MAX_RUN_S, RunBudget
from rudi_platform.runtime.handover import (
    MAX_HOPS,
    HandoverLimitExceeded,
    LambdaAsyncInvoker,
    S3CheckpointStore,
    hand_over,
    resume_state,
)
from rudi_platform.skills.contract import SkillRef, SkillRequest

FN = "meetrudi-tenant-dev-pool-skill-knowledge-ingest"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Context:
    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


class Budget(unittest.TestCase):
    def test_full_lambda_hands_over_at_12_and_stops_at_13_minutes(self):
        clock = Clock()
        budget = RunBudget.for_lambda(Context(900_000), clock)
        self.assertEqual(budget.run_s, MAX_RUN_S)
        clock.now += 719
        self.assertFalse(budget.should_hand_over())
        clock.now += 1
        self.assertTrue(budget.should_hand_over())
        self.assertFalse(budget.exhausted())
        clock.now += 60
        self.assertTrue(budget.exhausted())
        self.assertEqual(budget.remaining(), 0)

    def test_short_lambda_keeps_the_same_proportions(self):
        budget = RunBudget.for_lambda(Context(60_000), Clock())
        self.assertAlmostEqual(budget.run_s, 52.0)
        self.assertAlmostEqual(budget.lead_s, 4.0)

    def test_never_more_than_13_minutes(self):
        self.assertLessEqual(RunBudget.for_lambda(None, Clock()).run_s, 780)

    def test_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            RunBudget(0)
        with self.assertRaises(ValueError):
            RunBudget(60, 60)
        with self.assertRaises(ValueError):
            RunBudget.for_lambda(Context(0), Clock())


class Log:
    """Records calls from both fakes in one list, so the test can check their order."""

    def __init__(self):
        self.calls = []
        self.objects = {}


class FakeCheckpoints:
    def __init__(self, log):
        self.log = log

    def put(self, checkpoint_id, body):
        self.log.calls.append(("put", checkpoint_id))
        self.log.objects[checkpoint_id] = json.loads(json.dumps(body))

    def get(self, checkpoint_id):
        return self.log.objects[checkpoint_id]


class FakeInvoker:
    def __init__(self, log):
        self.log = log
        self.payloads = []

    def invoke_async(self, function_name, payload):
        self.log.calls.append(("invoke", function_name))
        self.payloads.append(json.loads(json.dumps(payload)))


def first_request():
    return SkillRequest(
        skill=SkillRef.parse("knowledge-ingest@1.0.0"),
        tenant=new_id(Cat.TENANT),
        channel="system",
        input={"library": new_id(Cat.LIBRARY), "page": 0},
        context={"pages_done": 0},
    )


class Handover(unittest.TestCase):
    def test_checkpoint_first_then_successor(self):
        log = Log()
        invoker = FakeInvoker(log)
        result = hand_over(
            first_request(),
            {"cursor": 120},
            {"page": 7},
            function_name=FN,
            checkpoints=FakeCheckpoints(log),
            invoker=invoker,
        )
        self.assertEqual([c[0] for c in log.calls], ["put", "invoke"])
        payload = invoker.payloads[0]
        self.assertEqual(payload["hop"], 1)
        self.assertEqual(payload["input"], {"page": 7})
        self.assertEqual(payload["context"], {"pages_done": 0})
        self.assertEqual(payload["job"], result.continuation.job)
        self.assertEqual(payload["checkpoint"], result.continuation.checkpoint)

    def test_successor_resumes_and_keeps_the_job(self):
        log = Log()
        checkpoints = FakeCheckpoints(log)
        invoker = FakeInvoker(log)
        hand_over(
            first_request(),
            {"cursor": 120},
            {"page": 7},
            function_name=FN,
            checkpoints=checkpoints,
            invoker=invoker,
        )
        payload = invoker.payloads[0]
        self.assertEqual(resume_state(payload, checkpoints), {"cursor": 120})
        second = SkillRequest.from_payload(payload)
        hand_over(
            second,
            {"cursor": 240},
            {"page": 14},
            function_name=FN,
            checkpoints=checkpoints,
            invoker=invoker,
        )
        self.assertEqual(invoker.payloads[1]["job"], payload["job"])
        self.assertEqual(invoker.payloads[1]["hop"], 2)

    def test_first_run_has_nothing_to_resume(self):
        self.assertIsNone(
            resume_state(first_request().to_payload(), FakeCheckpoints(Log()))
        )

    def test_checkpoint_of_another_job_is_refused(self):
        log = Log()
        checkpoints = FakeCheckpoints(log)
        invoker = FakeInvoker(log)
        hand_over(
            first_request(),
            {},
            {},
            function_name=FN,
            checkpoints=checkpoints,
            invoker=invoker,
        )
        payload = dict(invoker.payloads[0], job=new_id(Cat.JOB))
        with self.assertRaises(ValueError):
            resume_state(payload, checkpoints)

    def test_chain_is_capped(self):
        from dataclasses import replace

        log = Log()
        req = replace(first_request(), job=new_id(Cat.JOB), hop=MAX_HOPS)
        with self.assertRaises(HandoverLimitExceeded):
            hand_over(
                req,
                {},
                {},
                function_name=FN,
                checkpoints=FakeCheckpoints(log),
                invoker=FakeInvoker(log),
            )
        self.assertEqual(log.calls, [])


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType, Tagging=""):
        self.objects[(Bucket, Key)] = Body
        self.tags = getattr(self, "tags", {})
        self.tags[(Bucket, Key)] = Tagging

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)


class Adapters(unittest.TestCase):
    def test_s3_checkpoint_store_writes_inside_the_tenant_folder(self):
        s3 = FakeS3()
        bucket = "meetrudi-tenant-dev-pool-data-949753869755"
        store = S3CheckpointStore(s3, bucket, tenant_slug="cm")
        chk = new_id(Cat.CHECKPOINT)
        store.put(chk, {"job": "x", "state": {"n": 1}})
        key = (bucket, f"tenants/cm/runtime/checkpoints/{chk}.json")
        self.assertIn(key, s3.objects)
        self.assertEqual(s3.tags[key], "retention=checkpoint")
        self.assertEqual(store.get(chk)["state"], {"n": 1})
        with self.assertRaises(ValueError):
            store.put("not-an-id", {})

    def test_a_platform_job_checkpoints_under_platform(self):
        s3 = FakeS3()
        store = S3CheckpointStore(s3, "b", tenant_slug=None)
        chk = new_id(Cat.CHECKPOINT)
        store.put(chk, {"job": "x", "state": {}})
        self.assertIn(("b", f"platform/runtime/checkpoints/{chk}.json"), s3.objects)

    def test_the_owner_must_be_named_and_valid(self):
        with self.assertRaises(TypeError):
            S3CheckpointStore(FakeS3(), "b")  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            S3CheckpointStore(FakeS3(), "b", tenant_slug="CM/../other")

    def test_lambda_invoker_is_fire_and_forget(self):
        client = FakeLambda()
        LambdaAsyncInvoker(client).invoke_async(FN, {"a": 1})
        self.assertEqual(client.calls[0]["InvocationType"], "Event")
        self.assertEqual(client.calls[0]["FunctionName"], FN)


if __name__ == "__main__":
    unittest.main()
