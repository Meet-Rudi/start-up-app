"""
Gateway cascade tests — 429 retry/backoff and rate-limit fail-over. No network, no real LLM.

boto3 is stubbed before import (gateway builds boto3 clients at module load). time.sleep is
neutralized so backoff adds no wall-clock. Synthetic only (§8).

Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: object()
sys.modules["boto3"] = boto3_stub
os.environ["DATA_BUCKET"] = "meetrudi-ai-data-test"

import gateway  # noqa: E402

# Capture the REAL generate() now — other test modules (test_responder) replace the module-level
# gateway.generate with a fake at import time, so we call our captured reference directly.
_REAL_GENERATE = gateway.generate


class GatewayRetryTests(unittest.TestCase):
    def setUp(self):
        self._call = gateway._registry.call
        self._load = gateway._load_endpoints
        self._sleep = gateway.time.sleep
        gateway.time.sleep = lambda s: None            # no real backoff wait
        gateway._load_endpoints = lambda: []           # cascade = [GROQ_FALLBACK] only

    def tearDown(self):
        gateway._registry.call = self._call
        gateway._load_endpoints = self._load
        gateway.time.sleep = self._sleep

    def test_retries_same_provider_then_succeeds(self):
        calls = {"n": 0}

        def call(cfg, messages, timeout=25, json_mode=False):
            calls["n"] += 1
            if calls["n"] < 2:
                raise gateway.RateLimitError("429")
            return "ok"

        gateway._registry.call = call
        r = _REAL_GENERATE([{"role": "user", "content": "hi"}])
        self.assertEqual(r["text"], "ok")
        self.assertEqual(calls["n"], 2)                # failed once, retried, succeeded

    def test_persistent_429_exhausts_retries_then_raises(self):
        calls = {"n": 0}

        def call(cfg, messages, timeout=25, json_mode=False):
            calls["n"] += 1
            raise gateway.RateLimitError("429")

        gateway._registry.call = call
        with self.assertRaises(gateway.AllRateLimited):
            _REAL_GENERATE([{"role": "user", "content": "hi"}])
        self.assertEqual(calls["n"], gateway.MAX_RETRIES + 1)   # initial try + MAX_RETRIES

    def test_non_429_does_not_retry(self):
        calls = {"n": 0}

        def call(cfg, messages, timeout=25, json_mode=False):
            calls["n"] += 1
            raise gateway.AIError("boom")

        gateway._registry.call = call
        with self.assertRaises(gateway.AIError):
            _REAL_GENERATE([{"role": "user", "content": "hi"}])
        self.assertEqual(calls["n"], 1)                # no retry on a non-rate-limit error


if __name__ == "__main__":
    unittest.main()
