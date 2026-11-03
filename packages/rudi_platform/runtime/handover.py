"""
MEET_RUDI platform — handing long work to a successor run.

When a background (or turn) skill's RunBudget says to hand over, it saves its working state as a
checkpoint, then invokes the same function asynchronously with the same request, one hop further
on (blueprint H6, decision #32). The successor loads the checkpoint and carries on.

Order matters: checkpoint first, invoke second. If the invoke fails, an orphan checkpoint is
harmless and expires; the other order could start a successor with nothing to resume from.

The chain is capped at MAX_HOPS. A job that needs more than ~15 hours of 13-minute runs is a bug
or a loop, not a workload, and must stop loudly.

Clients are injected (S3 and Lambda via boto3 at runtime, fakes in tests); nothing here imports
boto3.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Protocol

from ..ids import Cat, new_id, require
from ..skills.contract import Continuation, SkillRequest, SkillResult

MAX_HOPS = 72
CHECKPOINT_PREFIX = "runtime/checkpoints"


class CheckpointStore(Protocol):
    def put(self, checkpoint_id: str, body: dict[str, Any]) -> None: ...
    def get(self, checkpoint_id: str) -> dict[str, Any]: ...


class AsyncInvoker(Protocol):
    def invoke_async(self, function_name: str, payload: dict[str, Any]) -> None: ...


class HandoverLimitExceeded(RuntimeError):
    pass


def hand_over(
    request: SkillRequest,
    state: dict[str, Any],
    next_input: dict[str, Any],
    *,
    function_name: str,
    checkpoints: CheckpointStore,
    invoker: AsyncInvoker,
) -> SkillResult:
    """Save `state`, start the successor run, and return the result this run should return.

    The successor receives the same request (context included) with `next_input`, the job id and
    hop + 1, plus a `checkpoint` key naming the saved state.
    """
    hop = request.hop + 1
    if hop > MAX_HOPS:
        raise HandoverLimitExceeded(f"job {request.job} reached {MAX_HOPS} hops")
    job = request.job or new_id(Cat.JOB)
    checkpoint = new_id(Cat.CHECKPOINT)
    checkpoints.put(
        checkpoint,
        {"job": job, "hop": hop, "skill": str(request.skill), "state": state},
    )
    successor = replace(request, input=next_input, job=job, hop=hop)
    payload = successor.to_payload()
    payload["checkpoint"] = checkpoint
    invoker.invoke_async(function_name, payload)
    return SkillResult(
        continuation=Continuation(
            checkpoint=checkpoint, job=job, hop=hop, next_input=next_input
        )
    )


def resume_state(
    payload: dict[str, Any], checkpoints: CheckpointStore
) -> dict[str, Any] | None:
    """The state a predecessor saved for this run, or None on a first run."""
    checkpoint = payload.get("checkpoint")
    if checkpoint is None:
        return None
    body = checkpoints.get(require(checkpoint, Cat.CHECKPOINT))
    if body.get("job") != payload.get("job"):
        raise ValueError(f"checkpoint {checkpoint} belongs to another job")
    return body.get("state") or {}


class S3CheckpointStore:
    """Checkpoints as JSON objects under runtime/checkpoints/ in a data bucket.

    Give the prefix a short lifecycle expiry (days, not months): a checkpoint is only useful to
    the next hop.
    """

    def __init__(
        self, s3_client: Any, bucket: str, prefix: str = CHECKPOINT_PREFIX
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")

    def _key(self, checkpoint_id: str) -> str:
        return f"{self._prefix}/{require(checkpoint_id, Cat.CHECKPOINT)}.json"

    def put(self, checkpoint_id: str, body: dict[str, Any]) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._key(checkpoint_id),
            Body=json.dumps(body).encode("utf-8"),
            ContentType="application/json",
        )

    def get(self, checkpoint_id: str) -> dict[str, Any]:
        obj = self._s3.get_object(Bucket=self._bucket, Key=self._key(checkpoint_id))
        return json.loads(obj["Body"].read())


class LambdaAsyncInvoker:
    """Fire-and-forget invoke (InvocationType=Event) through an injected boto3 Lambda client."""

    def __init__(self, lambda_client: Any) -> None:
        self._lambda = lambda_client

    def invoke_async(self, function_name: str, payload: dict[str, Any]) -> None:
        self._lambda.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
