"""
In-memory doubles for the Training Arena tests — no boto3, no network, synthetic data only.

FakeS3     the object API the arena uses, including multipart uploads, copies and signed URLs
FakeEngine meetrudi-engine-turn: answers turns from a script of replies (or errors)
FakeTransport  an OpenAI-compatible provider: answers from a queue, records what it was sent
"""

from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))


class _Body:
    def __init__(self, data: bytes):
        self._b = io.BytesIO(data)

    def read(self, n=-1):
        return self._b.read() if n is None or n < 0 else self._b.read(n)


class FakeS3:
    def __init__(self):
        self.store: dict = {}
        self.uploads: dict = {}

    def _b(self, bucket):
        return self.store.setdefault(bucket, {})

    def put_object(self, Bucket, Key, Body=b"", **_):
        self._b(Bucket)[Key] = Body if isinstance(Body, bytes) else str(Body).encode("utf-8")
        return {"ETag": "etag"}

    def get_object(self, Bucket, Key, **_):
        if Key not in self._b(Bucket):
            raise KeyError("NoSuchKey: " + Key)
        return {"Body": _Body(self._b(Bucket)[Key]), "VersionId": "v1"}

    def head_object(self, Bucket, Key, **_):
        if Key not in self._b(Bucket):
            raise KeyError("404")
        return {"ContentLength": len(self._b(Bucket)[Key])}

    def delete_object(self, Bucket, Key, **_):
        self._b(Bucket).pop(Key, None)
        return {}

    def copy_object(self, Bucket, Key, CopySource, **_):
        self._b(Bucket)[Key] = self._b(CopySource["Bucket"])[CopySource["Key"]]
        return {}

    def list_objects_v2(self, Bucket, Prefix="", Delimiter="", StartAfter="", ContinuationToken="", MaxKeys=1000, **_):
        keys = sorted(k for k in self._b(Bucket) if k.startswith(Prefix))
        start = ContinuationToken or StartAfter
        if start:
            keys = [k for k in keys if k > start]
        contents, common, seen = [], [], set()
        for k in keys:
            if Delimiter:
                rest = k[len(Prefix):]
                if Delimiter in rest:
                    cp = Prefix + rest.split(Delimiter, 1)[0] + Delimiter
                    if cp not in seen:
                        seen.add(cp)
                        common.append({"Prefix": cp})
                    continue
            contents.append({"Key": k, "Size": len(self._b(Bucket)[k])})
        truncated = len(contents) > MaxKeys
        contents = contents[:MaxKeys]
        out = {"Contents": contents, "IsTruncated": truncated}
        if common:
            out["CommonPrefixes"] = common
        if truncated:
            out["NextContinuationToken"] = contents[-1]["Key"]
        return out

    def create_multipart_upload(self, Bucket, Key, **_):
        uid = "up-%d" % (len(self.uploads) + 1)
        self.uploads[uid] = {"bucket": Bucket, "key": Key, "parts": {}}
        return {"UploadId": uid}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body, **_):
        self.uploads[UploadId]["parts"][PartNumber] = Body
        return {"ETag": "p%d" % PartNumber}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload, **_):
        up = self.uploads.pop(UploadId)
        self._b(Bucket)[Key] = b"".join(up["parts"][p["PartNumber"]] for p in MultipartUpload["Parts"])
        return {}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=900, **_):
        return "https://signed.example/%s/%s?op=%s" % (Params["Bucket"], Params["Key"], op)

    def text(self, bucket, key):
        return self._b(bucket)[key].decode("utf-8")


class FakeSecretsClient:
    def __init__(self, values=None):
        self.values = values or {}

    def get_secret_value(self, SecretId):
        if SecretId not in self.values:
            raise KeyError(SecretId)
        v = self.values[SecretId]
        return {"SecretString": v if isinstance(v, str) else json.dumps(v)}


class FakeEngine:
    """meetrudi-engine-turn. `replies` is a list consumed one per turn: a string (a reply), a dict
    (merged into the response), or an error code string prefixed with '!' (e.g. '!rate_limited')."""

    def __init__(self, replies=None, phases=None, timeouts=0):
        self.replies = list(replies or [])
        self.phases = list(phases or [])
        self.calls = []
        self.timeouts = timeouts

    def __call__(self, payload, timeout=60):
        self.calls.append(payload)
        op = payload.get("op")
        if op == "open":
            return {"ok": True, "reply": "Hi, I'm Rudi.", "state": {"phase": "learn"}, "vault": {},
                    "info": {"phase": "learn"}, "usage": {"model": "none", "tokens_in": 0, "tokens_out": 0},
                    "engine": {"release": "abc1234", "assets": {"prompts/rudi_guardrails.md": "v9"}}, "ms": 0}
        if op == "scan":
            from arena_format import local_identifiers
            return {"ok": True, "found": [local_identifiers(t) for t in payload["texts"]]}
        if op == "info":
            return {"ok": True, "release": "abc1234", "personalities": ["seed-rudi-v2", "seed-rudi-v3"]}
        if self.timeouts:
            self.timeouts -= 1
            from runner import EngineTimeout
            raise EngineTimeout()
        r = self.replies.pop(0) if self.replies else "Okay."
        if isinstance(r, str) and r.startswith("!"):
            return {"ok": False, "error": r[1:], "detail": "fake " + r[1:]}
        phase = self.phases.pop(0) if self.phases else "goal"
        before = (payload.get("state") or {}).get("phase", "learn")
        out = {"ok": True, "reply": r if isinstance(r, str) else "Okay.", "state": {"phase": phase, "goal": "walk more"},
               "vault": {"mapping": {}}, "info": {"phase_before": before, "phase": phase, "signals": {}, "model": "groq-gpt-oss-120b",
                                                    "lang": payload.get("locale"), "leak_blocked": False, "pii_redacted": {}},
               "usage": {"model": "groq-gpt-oss-120b", "tokens_in": 3000, "tokens_out": 200, "estimated": False},
               "engine": {"release": "abc1234", "assets": {"prompts/rudi_guardrails.md": "v9"}}, "ms": 1200}
        if isinstance(r, dict):
            out.update(r)
        return out


class FakeTransport:
    """An OpenAI-compatible provider. `queue` holds, per call: a dict (the JSON body to answer with),
    an Exception to raise, or ('429', headers)."""

    def __init__(self, queue=None, headers=None):
        self.queue = list(queue or [])
        self.sent = []
        self.headers = headers or {}

    def post(self, url, key, payload, timeout):
        self.sent.append({"url": url, "key": key, "payload": payload})
        item = self.queue.pop(0) if self.queue else chat({"message": "fine", "pushing": False})
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item[0] == "429":
            import arena_llm
            raise arena_llm._RateLimited(item[1])
        return item, dict(self.headers)


def chat(obj, tin=1000, tout=60):
    """A chat-completions body whose content is `obj` as JSON."""
    content = obj if isinstance(obj, str) else json.dumps(obj)
    return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


GROQ = "https://api.groq.com/openai/v1/chat/completions"
OVH = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions"


def cascades():
    return {
        "rudi": [{"name": "groq-gpt-oss-120b", "model": "openai/gpt-oss-120b", "kind": "groq", "endpoint": GROQ,
                  "key": "meetrudi/arena/rudi-groq", "on": True, "in": 0.13, "out": 0.52},
                 {"name": "ovh-gpt-oss-120b", "model": "gpt-oss-120b", "endpoint": OVH, "key": "meetrudi/arena/rudi-ovh",
                  "on": True, "in": 0.08, "out": 0.40}],
        "tester": [{"name": "tester-groq-qwen3.8-27b", "model": "qwen/qwen3.8-27b", "endpoint": GROQ,
                    "key": "meetrudi-groq-tester_and_judge", "on": True, "in": 0.0, "out": 0.0,
                    "limits": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000}, "max_tokens_param": "max_completion_tokens"},
                   {"name": "tester-ovh-llama-3.3-70b", "model": "Meta-Llama-3_3-70B-Instruct", "endpoint": OVH,
                    "key": "meetrudi/arena/sim-ovh", "on": True, "in": 0.67, "out": 0.67, "limits": {"rpm": 400}}],
        "judge": [{"name": "judge-groq-gpt-oss-120b", "model": "openai/gpt-oss-120b", "endpoint": GROQ,
                   "key": "meetrudi-groq-tester_and_judge", "on": True, "in": 0.0, "out": 0.0,
                   "limits": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000}},
                  {"name": "judge-ovh-gpt-oss-120b", "model": "gpt-oss-120b", "endpoint": OVH,
                   "key": "meetrudi/arena/sim-ovh", "on": True, "in": 0.08, "out": 0.40}],
    }
