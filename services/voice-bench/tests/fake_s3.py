"""
Minimal in-memory S3 double for unit tests — supports just the object API ConversationStore
uses (put_object, get_object, list_objects_v2 with Prefix/Delimiter/StartAfter/pagination).

No boto3, no moto, no network. Synthetic data only (no real PII — CLAUDE.md §8).
"""

from __future__ import annotations

import io
from typing import Any


class _NoSuchKey(Exception):
    pass


class _Body:
    def __init__(self, data: bytes) -> None:
        self._b = io.BytesIO(data)

    def read(self) -> bytes:
        return self._b.read()


class FakeS3:
    def __init__(self) -> None:
        # {bucket: {key: bytes}}
        self._store: dict[str, dict[str, bytes]] = {}

    # -- API surface used by ConversationStore -----------------------------
    def put_object(self, Bucket: str, Key: str, Body: bytes, **_: Any) -> dict[str, Any]:
        self._store.setdefault(Bucket, {})[Key] = Body if isinstance(Body, bytes) else Body.encode()
        return {}

    def get_object(self, Bucket: str, Key: str, **_: Any) -> dict[str, Any]:
        try:
            data = self._store[Bucket][Key]
        except KeyError:
            raise _NoSuchKey("NoSuchKey: %s" % Key)
        return {"Body": _Body(data)}

    def list_objects_v2(self, Bucket: str, Prefix: str = "", Delimiter: str = "",
                        StartAfter: str = "", ContinuationToken: str = "",
                        MaxKeys: int = 1000, **_: Any) -> dict[str, Any]:
        keys = sorted(k for k in self._store.get(Bucket, {}) if k.startswith(Prefix))
        start = ContinuationToken or StartAfter
        if start:
            keys = [k for k in keys if k > start]

        contents: list[dict[str, Any]] = []
        common: list[str] = []
        seen_prefixes: set[str] = set()
        for k in keys:
            if Delimiter:
                rest = k[len(Prefix):]
                if Delimiter in rest:
                    cp = Prefix + rest.split(Delimiter, 1)[0] + Delimiter
                    if cp not in seen_prefixes:
                        seen_prefixes.add(cp)
                        common.append({"Prefix": cp})
                    continue
            contents.append({"Key": k})

        truncated = len(contents) > MaxKeys
        contents = contents[:MaxKeys]
        resp: dict[str, Any] = {"Contents": contents, "IsTruncated": truncated}
        if common:
            resp["CommonPrefixes"] = common
        if truncated and contents:
            resp["NextContinuationToken"] = contents[-1]["Key"]
        return resp
