"""
The model gateway — cascade, retries, time budget, de-identification, policy and metering.

No network and no real model: a scripted transport plays each provider, and a fake clock makes
backoff and budgets exact. Synthetic data only.

What this guards:
  - a 429 retried, a hard failure failed over, AllRateLimited only when every provider was limited;
  - a call that runs out of budget stops instead of becoming dead air;
  - empty content and JSON cut off by the token cap fail over instead of reaching a person;
  - a name never reaching the provider, and coming back restored;
  - a tenant's EU-only policy never quietly widened;
  - every call metered once, with its tags.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.gateway import (
    AIError,
    AllRateLimited,
    ModelEndpoint,
    ModelGateway,
    ModelPolicy,
    PolicyError,
    RateLimitError,
    SecretsManagerResolver,
    StaticSecrets,
    UrllibTransport,
    UsageTags,
    load_endpoints,
    parse_envelope,
    resolve_cascade,
)
from rudi_platform.gateway.core import MIN_ATTEMPT_S
from rudi_platform.gateway.secrets import extract_key
from rudi_platform.ids import Cat, is_id, new_id
from rudi_platform.privacy.deid import AliasVault, HeuristicDetector


def ep(name, region="eu", dpa=True, **kw):
    return ModelEndpoint(
        name=name,
        url=f"https://{name}.example/v1/chat/completions",
        model=f"model-{name}",
        secret=f"meetrudi/internal/dev/{name}",
        region=region,
        dpa=dpa,
        **kw,
    )


def ok(text, tokens_in=100, tokens_out=20, finish="stop"):
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ScriptedTransport:
    """Each endpoint URL plays its script in order: a response dict, or an exception to raise."""

    def __init__(self, clock, scripts, latency=0.1):
        self.clock = clock
        self.scripts = {url: list(steps) for url, steps in scripts.items()}
        self.latency = latency
        self.calls = []

    def post_json(self, url, headers, payload, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout}
        )
        self.clock.now += self.latency
        step = self.scripts[url].pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class Sink:
    def __init__(self):
        self.records = []

    def record(self, rec):
        self.records.append(rec)


A, B, C = ep("a"), ep("b"), ep("c")
TENANT = new_id(Cat.TENANT)
TAGS = UsageTags(tenant=TENANT, skill="goal-setting@1.0.0", channel="whatsapp")
MSGS = [
    {"role": "system", "content": "You are a coach."},
    {"role": "user", "content": "hi"},
]


def gateway(scripts, latency=0.1, **kw):
    clock = Clock()
    transport = ScriptedTransport(clock, scripts, latency)
    sink = Sink()
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.now += s

    gw = ModelGateway(transport, Keys(), sink=sink, sleep=sleep, clock=clock, **kw)
    return gw, transport, sink, sleeps, clock


class Keys:
    """A key for every test endpoint except "stranger", whose secret does not exist."""

    def api_key(self, secret_name):
        name = secret_name.rsplit("/", 1)[-1]
        if name == "stranger":
            raise KeyError(secret_name)
        return f"key-{name}"


class Cascade(unittest.TestCase):
    def test_first_usable_answer_wins_and_is_metered(self):
        gw, transport, sink, _, _ = gateway({A.url: [ok("hello")]})
        gen = gw.generate(MSGS, [A, B], tags=TAGS)
        self.assertEqual((gen.text, gen.endpoint, gen.attempts), ("hello", "a", 1))
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer key-a")
        self.assertEqual(len(sink.records), 1)
        rec = sink.records[0]
        self.assertTrue(is_id(rec.id, Cat.USAGE))
        self.assertEqual(
            (rec.outcome, rec.endpoint, rec.tokens_in, rec.tokens_out),
            ("ok", "a", 100, 20),
        )
        self.assertEqual(rec.tags, TAGS)

    def test_429_is_retried_with_backoff_then_succeeds(self):
        gw, transport, _, sleeps, _ = gateway(
            {A.url: [RateLimitError("429"), ok("hi")]}
        )
        self.assertEqual(gw.generate(MSGS, [A]).text, "hi")
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(len(transport.calls), 2)

    def test_persistent_429_everywhere_is_all_rate_limited(self):
        gw, transport, sink, sleeps, _ = gateway(
            {A.url: [RateLimitError("429")] * 3, B.url: [RateLimitError("429")] * 3}
        )
        with self.assertRaises(AllRateLimited):
            gw.generate(MSGS, [A, B], tags=TAGS)
        self.assertEqual(len(transport.calls), 6)  # (1 try + 2 retries) per endpoint
        self.assertEqual(sleeps, [0.5, 1.0, 0.5, 1.0])
        self.assertEqual([r.outcome for r in sink.records], ["rate_limited"])

    def test_hard_failure_fails_over_without_retry(self):
        gw, _, _, sleeps, _ = gateway(
            {A.url: [AIError("HTTP 500")], B.url: [ok("from b")]}
        )
        gen = gw.generate(MSGS, [A, B])
        self.assertEqual((gen.text, gen.endpoint, gen.attempts), ("from b", "b", 2))
        self.assertEqual(sleeps, [])

    def test_a_mix_of_failures_is_a_plain_ai_error(self):
        gw, _, sink, _, _ = gateway(
            {A.url: [RateLimitError("429")] * 3, B.url: [AIError("500")]}
        )
        with self.assertRaises(AIError) as caught:
            gw.generate(MSGS, [A, B])
        self.assertNotIsInstance(caught.exception, AllRateLimited)
        self.assertEqual(sink.records[-1].outcome, "failed")

    def test_unusable_answers_fail_over(self):
        gw, _, _, _, _ = gateway({
            A.url: [ok("")],
            B.url: [ok('{"reply": "half', finish="length")],
            C.url: [ok('{"reply": "whole", "signals": {}}')],
        })  # fmt: skip
        gen = gw.generate(MSGS, [A, B, C], json_mode=True)
        self.assertEqual(gen.endpoint, "c")

    def test_a_capped_answer_is_fine_outside_json_mode(self):
        gw, _, _, _, _ = gateway({A.url: [ok("a long answer, cut", finish="length")]})
        self.assertEqual(gw.generate(MSGS, [A]).text, "a long answer, cut")

    def test_a_missing_key_fails_over(self):
        stranger = ep("stranger")
        gw, _, _, _, _ = gateway({B.url: [ok("b")]})
        self.assertEqual(gw.generate(MSGS, [stranger, B]).endpoint, "b")

    def test_request_shape(self):
        gw, transport, _, _, _ = gateway({A.url: [ok("{}")]})
        gw.generate(MSGS, [A], json_mode=True, max_tokens=250)
        payload = transport.calls[0]["payload"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 250)
        self.assertEqual(payload["model"], "model-a")

    def test_malformed_calls_are_refused(self):
        gw, _, _, _, _ = gateway({})
        with self.assertRaises(ValueError):
            gw.generate(MSGS, [])
        with self.assertRaises(ValueError):
            gw.generate(MSGS, [A], vault=AliasVault())


class Budget(unittest.TestCase):
    def test_attempt_timeout_never_outlasts_the_budget(self):
        slow = ep("slow", timeout_s=7)
        gw, transport, _, _, _ = gateway({slow.url: [ok("x")]})
        gw.generate(MSGS, [slow], budget_s=5)
        self.assertEqual(transport.calls[0]["timeout"], 5)

    def test_stops_when_too_little_budget_is_left(self):
        gw, transport, sink, _, _ = gateway(
            {A.url: [AIError("stall")], B.url: [ok("b")]}, latency=4.0
        )
        with self.assertRaises(AIError):
            gw.generate(MSGS, [A, B], budget_s=5)
        self.assertEqual(
            len(transport.calls), 1
        )  # 1 s left < MIN_ATTEMPT_S: B is never tried
        self.assertLess(5 - 4.0, MIN_ATTEMPT_S)
        self.assertEqual(sink.records[-1].outcome, "out_of_budget")

    def test_no_backoff_when_it_would_eat_the_budget(self):
        gw, _, _, sleeps, _ = gateway(
            {A.url: [RateLimitError("429")], B.url: [ok("b")]}, latency=1.0
        )
        # At t=1.0 a 0.5 s backoff would leave 1.9 s (< MIN_ATTEMPT_S), so A is not retried.
        self.assertEqual(gw.generate(MSGS, [A, B], budget_s=3.4).endpoint, "b")
        self.assertEqual(sleeps, [])


class DeIdentification(unittest.TestCase):
    def test_names_never_reach_the_provider_and_come_back(self):
        gw, transport, _, _, _ = gateway(
            {
                A.url: [
                    ok(
                        '{"reply": "Did <| Person_A |> join you?", "signals": {"buddy": "<| Person_A |>"}}'
                    )
                ]
            }
        )
        vault = AliasVault()
        det = HeuristicDetector(keep=frozenset({"Emma"}))
        msgs = [
            {"role": "system", "content": "You are Emma, a coach."},
            {"role": "user", "content": "I went running with Peter"},
        ]
        gen = gw.generate(msgs, [A], json_mode=True, vault=vault, detector=det)
        sent = json.dumps(transport.calls[0]["payload"]["messages"])
        self.assertNotIn("Peter", sent)
        self.assertIn("Emma", sent)  # the persona's own name is kept
        self.assertIn("<| Person_A |>", sent)
        env = parse_envelope(gen.text)
        self.assertEqual(env.reply, "Did Peter join you?")
        self.assertEqual(env.signals, {"buddy": "Peter"})
        self.assertTrue(gen.masked)

    def test_an_unknown_alias_never_reaches_a_person(self):
        gw, _, _, _, _ = gateway({A.url: [ok("Say hi to <| Person_Q |>!")]})
        gen = gw.generate(MSGS, [A], vault=AliasVault(), detector=HeuristicDetector())
        self.assertEqual(gen.text, "Say hi to them!")


class Policy(unittest.TestCase):
    def test_policy_filters_in_order(self):
        us = ep("us", region="us", dpa=False)
        eu_no_dpa = ep("eu-nodpa", dpa=False)
        cheap = ep("cheap", tiers=frozenset({"classify"}))
        all_eps = [us, A, eu_no_dpa, cheap, B]
        self.assertEqual(resolve_cascade(all_eps, ModelPolicy()), [us, A, eu_no_dpa, B])
        self.assertEqual(resolve_cascade(all_eps, ModelPolicy.eu_only()), [A, B])
        self.assertEqual(
            resolve_cascade(all_eps, ModelPolicy.eu_only(), tier="classify"),
            [A, cheap, B],
        )
        only_b = ModelPolicy(endpoints=frozenset({"b"}))
        self.assertEqual(resolve_cascade(all_eps, only_b), [B])

    def test_an_empty_cascade_fails_closed(self):
        with self.assertRaises(PolicyError):
            resolve_cascade([ep("us", region="us", dpa=False)], ModelPolicy.eu_only())
        with self.assertRaises(PolicyError):
            resolve_cascade([ep("off", enabled=False)], ModelPolicy())

    def test_legacy_config_loads_untagged(self):
        config = {
            "_comment": "ignored",
            "endpoints": [
                {"name": "groq-gpt-oss-120b", "kind": "groq", "endpoint": "https://api.groq.com/x",
                 "model": "openai/gpt-oss-120b", "secret": "s1", "enabled": True},
                {"name": "mistral-small", "kind": "openai_compatible", "endpoint": "https://m/x",
                 "model": "m", "secret": "s2", "enabled": False, "_note": "off"},
            ],
        }  # fmt: skip
        eps = load_endpoints(config)
        self.assertEqual([e.name for e in eps], ["groq-gpt-oss-120b", "mistral-small"])
        self.assertEqual(
            (eps[0].region, eps[0].dpa, eps[0].url),
            ("other", False, "https://api.groq.com/x"),
        )
        self.assertEqual(resolve_cascade(eps, ModelPolicy()), [eps[0]])
        with self.assertRaises(
            PolicyError
        ):  # untagged endpoints never pass an EU-only policy
            resolve_cascade(eps, ModelPolicy.eu_only())

    def test_tagged_config_and_cost(self):
        e = ModelEndpoint.from_config({
            "name": "ovh-120b", "endpoint": "https://ovh/x", "model": "gpt-oss-120b", "secret": "s",
            "region": "eu", "dpa": True, "tiers": ["coach", "long"], "timeout": 7, "max_tokens": 300,
            "price_in_eur_per_mtok": 0.08, "price_out_eur_per_mtok": 0.40,
        })  # fmt: skip
        self.assertEqual(
            (e.timeout_s, e.max_tokens, e.tiers),
            (7.0, 300, frozenset({"coach", "long"})),
        )
        self.assertAlmostEqual(e.cost_eur(1_000_000, 500_000), 0.28)

    def test_bad_endpoints_are_refused(self):
        for kw in (
            {"region": "mars"},
            {"kind": "carrier-pigeon"},
            {"tiers": frozenset({"poetry"})},
            {"url": ""},
        ):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                ModelEndpoint(
                    **{
                        "name": "x",
                        "url": "https://x",
                        "model": "m",
                        "secret": "s",
                        **kw,
                    }
                )


class Envelope(unittest.TestCase):
    def test_parsing(self):
        e = parse_envelope('{"reply": "Hi!", "signals": {"lang": "nl"}}')
        self.assertEqual((e.reply, e.signals, e.parsed), ("Hi!", {"lang": "nl"}, True))
        e = parse_envelope('Sure: {"reply": "Hi!", "signals": {}} hope that helps')
        self.assertEqual((e.reply, e.parsed), ("Hi!", True))
        e = parse_envelope("Just prose.")
        self.assertEqual((e.reply, e.signals, e.parsed), ("Just prose.", {}, False))
        e = parse_envelope('{"reply": "", "signals": ["not", "a", "dict"]}')
        self.assertEqual(e.signals, {})
        self.assertEqual(e.reply, '{"reply": "", "signals": ["not", "a", "dict"]}')


class Headroom(unittest.TestCase):
    def test_headroom(self):
        gw, _, _, _, _ = gateway({A.url: [ok("ok")], B.url: [AIError("down")]})
        self.assertTrue(gw.has_headroom([A]))
        self.assertFalse(gw.has_headroom([B]))


class SecretsAndTransport(unittest.TestCase):
    def test_key_shapes(self):
        self.assertEqual(extract_key("sk-plain"), "sk-plain")
        self.assertEqual(extract_key('{"api_key": "sk-1"}'), "sk-1")
        self.assertEqual(extract_key('{"API_KEY": "sk-2", "note": "x"}'), "sk-2")
        self.assertEqual(extract_key('{"whatever": "sk-3"}'), "sk-3")
        self.assertEqual(StaticSecrets({"s": "sk-4"}).api_key("s"), "sk-4")

    def test_secrets_manager_is_read_once(self):
        class Client:
            calls = 0

            def get_secret_value(self, SecretId):
                Client.calls += 1
                return {"SecretString": '{"key": "sk-x"}'}

        resolver = SecretsManagerResolver(Client())
        self.assertEqual(resolver.api_key("s"), "sk-x")
        self.assertEqual(resolver.api_key("s"), "sk-x")
        self.assertEqual(Client.calls, 1)

    def test_urllib_transport_classifies_failures(self):
        def opener_raising(exc):
            def opener(req, timeout):
                raise exc

            return opener

        def http_error(code):
            return urllib.error.HTTPError(
                "https://x", code, "err", {}, io.BytesIO(b"detail")
            )

        with self.assertRaises(RateLimitError):
            UrllibTransport(opener_raising(http_error(429))).post_json(
                "https://x", {}, {}, 1
            )
        with self.assertRaises(AIError) as caught:
            UrllibTransport(opener_raising(http_error(500))).post_json(
                "https://x", {}, {}, 1
            )
        self.assertNotIsInstance(caught.exception, RateLimitError)
        with self.assertRaises(AIError):
            UrllibTransport(opener_raising(urllib.error.URLError("dns"))).post_json(
                "https://x", {}, {}, 1
            )

    def test_urllib_transport_sends_a_browser_user_agent(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            seen["ua"] = req.get_header("User-agent")
            seen["timeout"] = timeout
            return Resp(b'{"choices": []}')

        self.assertEqual(
            UrllibTransport(opener).post_json("https://x", {}, {"a": 1}, 3),
            {"choices": []},
        )
        self.assertIn("Mozilla", seen["ua"])
        self.assertEqual(seen["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
