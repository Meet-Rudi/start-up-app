"""
meetrudi-internal-prod-prospect-intake — the adoption request form's endpoint.

What this guards: a request that stops halfway still leaves its contact details; partial and
sent requests are told apart by their key; nobody reads a step before the ones before it are
saved, or a request without its token; the questionnaire stays something the browser and the
server read the same way; and no contact detail reaches a log line. Synthetic data only (§8).

Run:  python -m unittest discover -s services/prospect-intake/tests -v
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "packages"))
sys.path.insert(0, os.path.join(HERE, "..", "src"))


class _S3Error(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deny_delete = False

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _S3Error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        if self.deny_delete:
            raise _S3Error("AccessDenied")
        self.objects.pop(Key, None)
        return {}

    def record(self, key):
        return json.loads(self.objects[key])


class FakeSES:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail = False

    def send_email(self, **kw):
        if self.fail:
            raise _S3Error("MessageRejected")
        self.sent.append(kw)
        return {"MessageId": f"msg-{len(self.sent)}"}


S3, SES = FakeS3(), FakeSES()
_boto3 = types.ModuleType("boto3")
_boto3.client = lambda name, *a, **k: SES if name == "ses" else S3
sys.modules["boto3"] = _boto3
_config = types.ModuleType("botocore.config")
_config.Config = lambda **k: None
sys.modules["botocore"] = types.ModuleType("botocore")
sys.modules["botocore.config"] = _config
os.environ.update(
    {
        "DATA_BUCKET": "meetrudi-ai-data-test",
        "MAIL_FROM": "sender@example.org",
        "MAIL_FROM_NAME": "Rudi request form",
        "NOTIFY_TO": "team@example.org",
        "NOTIFY_CC": "copy@example.org",
    }
)

import handler
import notify
import questionnaire as qn
from rudi_platform.ids import Cat, is_id
from rudi_platform.naming import internal_name

CONTACT = {
    "organisation": "Example Health Lab",
    "contact_name": "Alex Example",
    "contact_role": "Programme lead",
    "contact_email": "alex@example.org",
    "contact_phone": "+32 470 00 00 00",
    "country": "be",
    "contact_agreement": True,
}
STEPS = {
    "programme": {
        "organisation_kind": "clinic",
        "purpose": "Keep moving after rehab, every week.",
        "audience": "adults after our programme",
        "pilot_size": "25",
        "languages": ["en", "nl"],
        "start_when": "quarter",
    },
    "channels": {"channels": ["whatsapp", "api"], "wa_number": "rudi"},
    "skills": {"skills": ["goal-setting"]},
    "persona": {
        "persona_name": "Rudi",
        "tone_energy": 3,
        "tone_directness": 2,
        "tone_structure": 3,
        "cadence": "weekly",
    },
    "oversight": {
        "involvement": "active",
        "step_in": ["read_conversations", "take_over"],
        "coverage": "office",
        "alerts": ["quiet"],
        "alert_route": ["email"],
    },
    "insights": {"dashboards": ["engagement", "goals"], "team_size": "2-5"},
    "send": {
        "material": "later",
        "ai_supply": "own",
        "ai_providers": ["azure_openai"],
        "ai_eu_hosted": "yes",
        "end_user_login": "unsure",
        "dpa": True,
        "scope": True,
        "updates": False,
    },
}
PII = ("Example Health Lab", "Alex Example", "alex@example.org", "470 00 00 00")


def call(payload, method="POST", raw=None):
    event = {
        "requestContext": {"http": {"method": method}},
        "body": raw if raw is not None else json.dumps(payload),
    }
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        resp = handler.handler(event, None)
    logs = out.getvalue()
    for word in PII:
        assert word not in logs, f"contact detail in a log line: {word!r}"
    return resp["statusCode"], json.loads(resp["body"])


def start(**over):
    answers = {**CONTACT, **over}
    return call(
        {"action": "start", "lang": "en", "answers": answers, "elapsed_ms": 30_000}
    )


class Base(unittest.TestCase):
    def setUp(self):
        S3.objects.clear()
        S3.deny_delete = False
        SES.sent.clear()
        SES.fail = False

    def started(self):
        status, body = start()
        self.assertEqual(status, 201, body)
        return body["id"], body["token"], body

    def save(self, pid, token, step, answers, draft=False):
        return call(
            {
                "action": "save",
                "id": pid,
                "token": token,
                "step": step,
                "answers": answers,
                "draft": draft,
            }
        )

    def walk(self, until=None):
        pid, token, _ = self.started()
        for key, answers in STEPS.items():
            if key == until:
                break
            status, body = self.save(pid, token, key, answers)
            self.assertEqual(status, 200, (key, body))
        return pid, token, body


class TheQuestionnaire(unittest.TestCase):
    def test_it_passes_its_own_check(self):
        qn.check(qn.load())

    def test_contact_is_first_and_the_steps_are_the_agreed_ones(self):
        keys = [s["key"] for s in handler.Q["steps"]]
        self.assertEqual(keys, ["contact", *STEPS])

    def test_check_refuses_a_condition_on_a_later_field(self):
        q = copy.deepcopy(handler.Q)
        q["steps"][1]["fields"][0]["show_if"] = {"field": "channels", "has": "voice"}
        with self.assertRaisesRegex(ValueError, "asked before"):
            qn.check(q)

    def test_check_refuses_a_repeated_key(self):
        q = copy.deepcopy(handler.Q)
        q["steps"][2]["fields"][0]["key"] = "purpose"
        with self.assertRaisesRegex(ValueError, "repeated field key"):
            qn.check(q)

    def test_check_refuses_a_default_it_would_not_accept(self):
        q = copy.deepcopy(handler.Q)
        q["steps"][3]["fields"][0]["default"] = ["no-such-skill"]
        with self.assertRaisesRegex(ValueError, "default refused"):
            qn.check(q)

    def test_a_step_can_branch_on_an_earlier_answer(self):
        q = copy.deepcopy(handler.Q)
        q["steps"].insert(
            2,
            {
                "key": "clinic",
                "title": "Clinical set-up",
                "icon": "shield-check",
                "show_if": {"field": "organisation_kind", "is": "clinic"},
                "fields": [{"key": "ward", "type": "text", "label": "Ward"}],
            },
        )
        qn.check(q)
        clinic = {"contact": {}, "programme": {"organisation_kind": "clinic"}}
        gym = {"contact": {}, "programme": {"organisation_kind": "programme"}}
        self.assertIn("clinic", [s["key"] for s in qn.applicable_steps(q, clinic)])
        self.assertNotIn("clinic", [s["key"] for s in qn.applicable_steps(q, gym)])

    def test_every_step_has_an_icon_the_page_draws(self):
        page = os.path.join(
            ROOT, "web", "adoption-request", "adoption-request.src.html"
        )
        with open(page, encoding="utf-8") as fh:
            source = fh.read()
        for name in qn.ICONS:
            self.assertIn(f'"{name}": \'<', source, f"the page does not draw {name}")
        chosen = [s["icon"] for s in handler.Q["steps"]]
        self.assertEqual(
            chosen,
            [
                "id-card",
                "users",
                "message-circle-more",
                "list-checks",
                "smile",
                "sliders-horizontal",
                "gauge",
                "send",
            ],
        )
        q = copy.deepcopy(handler.Q)
        q["steps"][1]["icon"] = "rocket"
        with self.assertRaisesRegex(ValueError, "not one the page draws"):
            qn.check(q)


class Naming(unittest.TestCase):
    def test_keys_say_partial_or_complete_in_folder_and_file(self):
        pid = "prs_" + "0" * 32
        self.assertEqual(
            handler.partial_key(pid), f"platform/prospects/partial/{pid}.partial.json"
        )
        self.assertEqual(
            handler.complete_key(pid),
            f"platform/prospects/complete/{pid}.complete.json",
        )

    def test_the_template_uses_the_platform_name(self):
        with open(os.path.join(HERE, "..", "template.yaml"), encoding="utf-8") as fh:
            template = fh.read()
        name = internal_name("prod", "prospect-intake")
        self.assertIn(f"FunctionName: {name}\n", template)
        self.assertIn(f"/aws/lambda/{name}\n", template)


class Open(Base):
    def test_open_shows_the_contact_step_and_no_other_question(self):
        status, body = call({"action": "open", "lang": "en"})
        self.assertEqual(status, 200)
        self.assertEqual(body["section"]["key"], "contact")
        self.assertEqual(len(body["steps"]), 8)
        text = json.dumps(body)
        for later in ("purpose", "involvement", "ai_supply", "dashboards"):
            self.assertNotIn(later, text)


class Start(Base):
    def test_start_stores_the_contact_details_as_a_partial(self):
        pid, token, body = self.started()
        self.assertTrue(is_id(pid, Cat.PROSPECT))
        self.assertEqual(body["next"]["section"]["key"], "programme")
        self.assertIsNone(body["next"]["answers"])
        self.assertEqual(list(S3.objects), [handler.partial_key(pid)])
        rec = S3.record(handler.partial_key(pid))
        self.assertEqual(rec["state"], "partial")
        self.assertEqual(rec["follow_up"]["contact_email"], "alex@example.org")
        self.assertTrue(rec["follow_up"]["may_contact"])
        self.assertEqual(rec["follow_up"]["stopped_before"], "programme")
        self.assertEqual(rec["progress"]["done"], ["contact"])
        self.assertTrue(rec["agreements"]["contact_agreement"]["granted"])
        self.assertNotIn(token, json.dumps(rec))

    def test_start_needs_an_email_and_the_agreement(self):
        for field, value in (
            ("contact_email", ""),
            ("contact_email", "not-an-address"),
            ("contact_agreement", False),
        ):
            status, body = start(**{field: value})
            self.assertEqual((status, body.get("field")), (422, field))
        self.assertEqual(S3.objects, {})

    def test_bots_get_a_success_and_store_nothing(self):
        for extra in ({"hp": "spam"}, {"elapsed_ms": 200}):
            payload = {
                "action": "start",
                "answers": CONTACT,
                "elapsed_ms": 30_000,
                **extra,
            }
            status, body = call(payload)
            self.assertEqual(status, 201)
            self.assertIsNone(body["next"])
        self.assertEqual(S3.objects, {})


class Access(Base):
    def test_a_request_needs_its_own_token(self):
        pid, _, _ = self.started()
        status, _ = self.save(pid, "wrong-token", "programme", STEPS["programme"])
        self.assertEqual(status, 403)
        status, _ = self.save("prs_not-an-id", "x", "programme", STEPS["programme"])
        self.assertEqual(status, 400)
        status, _ = self.save("prs_" + "1" * 32, "x", "programme", STEPS["programme"])
        self.assertEqual(status, 404)

    def test_no_step_can_be_read_or_saved_ahead_of_the_saved_ones(self):
        pid, token, _ = self.started()
        status, body = call(
            {"action": "load", "id": pid, "token": token, "step": "oversight"}
        )
        self.assertEqual(
            (status, body["error"], body["step"]), (403, "not_yet", "programme")
        )
        status, _ = self.save(pid, token, "persona", STEPS["persona"])
        self.assertEqual(status, 403)

    def test_a_saved_step_can_be_loaded_with_its_answers(self):
        pid, token, _ = self.walk(until="skills")
        status, body = call(
            {"action": "load", "id": pid, "token": token, "step": "programme"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["answers"]["purpose"], STEPS["programme"]["purpose"])


class Saving(Base):
    def test_a_draft_keeps_what_is_filled_in_without_finishing_the_step(self):
        pid, token, _ = self.started()
        status, body = self.save(
            pid,
            token,
            "programme",
            {"purpose": "Only this so far, half done."},
            draft=True,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(body["next"])
        rec = S3.record(handler.partial_key(pid))
        self.assertEqual(
            rec["answers"]["programme"], {"purpose": "Only this so far, half done."}
        )
        self.assertEqual(rec["progress"]["next_step"], "programme")
        status, body = call({"action": "resume", "id": pid, "token": token})
        self.assertEqual(
            (body["section"]["key"], body["answers"]["purpose"]),
            ("programme", "Only this so far, half done."),
        )

    def test_a_full_save_refuses_a_missing_answer(self):
        pid, token, _ = self.started()
        answers = dict(STEPS["programme"], audience="")
        status, body = self.save(pid, token, "programme", answers)
        self.assertEqual((status, body["field"]), (422, "audience"))

    def test_a_field_shown_by_a_choice_is_asked_only_then(self):
        pid, token, _ = self.walk(until="channels")
        status, body = self.save(pid, token, "channels", {"channels": ["whatsapp"]})
        self.assertEqual((status, body.get("field")), (422, "wa_number"))
        status, _ = self.save(
            pid,
            token,
            "channels",
            {
                "channels": ["voice"],
                "wa_number": "own",
                "call_window": {"from": "10:00", "to": "18:00"},
            },
        )
        self.assertEqual(status, 200)
        stored = S3.record(handler.partial_key(pid))["answers"]["channels"]
        self.assertNotIn("wa_number", stored)
        self.assertEqual(stored["call_window"], {"from": "10:00", "to": "18:00"})

    def test_a_call_window_must_end_after_it_starts(self):
        pid, token, _ = self.walk(until="channels")
        status, body = self.save(
            pid,
            token,
            "channels",
            {"channels": ["voice"], "call_window": {"from": "20:00", "to": "09:00"}},
        )
        self.assertEqual((status, body["detail"]), (422, "ends_before_start"))

    def test_the_safety_alert_is_always_on(self):
        pid, _token, _ = self.walk(until="insights")
        alerts = S3.record(handler.partial_key(pid))["answers"]["oversight"]["alerts"]
        self.assertEqual(alerts, ["safety", "quiet"])

    def test_an_earlier_answer_decides_what_a_later_step_asks(self):
        pid, token, body = self.walk(until="send")
        keys = [
            g["key"]
            for f in body["next"]["section"]["fields"]
            if f["type"] == "group"
            for g in f["fields"]
        ]
        self.assertIn("end_user_login", keys)  # channels include the API
        self.save(
            pid, token, "channels", {"channels": ["whatsapp"], "wa_number": "rudi"}
        )
        _status, body = call(
            {"action": "load", "id": pid, "token": token, "step": "send"}
        )
        keys = [
            g["key"]
            for f in body["section"]["fields"]
            if f["type"] == "group"
            for g in f["fields"]
        ]
        self.assertNotIn("end_user_login", keys)


class Completing(Base):
    def test_sending_moves_the_request_from_partial_to_complete(self):
        pid, token, body = self.walk()
        self.assertTrue(body["complete"])
        self.assertEqual(list(S3.objects), [handler.complete_key(pid)])
        rec = S3.record(handler.complete_key(pid))
        self.assertEqual(rec["state"], "complete")
        self.assertEqual(rec["progress"]["percent"], 100)
        self.assertIsNone(rec["follow_up"]["stopped_before"])
        self.assertEqual(set(rec["agreements"]), {"contact_agreement", "dpa", "scope"})
        self.assertEqual(rec["answers"]["send"]["ai_providers"], ["azure_openai"])
        status, body = call({"action": "resume", "id": pid, "token": token})
        self.assertEqual((status, body), (200, {"complete": True}))
        status, body = self.save(pid, token, "send", STEPS["send"])
        self.assertEqual((status, body["error"]), (409, "already_sent"))

    def test_without_the_delete_right_the_partial_says_it_is_complete(self):
        S3.deny_delete = True
        pid, token, body = self.walk()
        self.assertTrue(body["complete"])
        self.assertEqual(S3.record(handler.partial_key(pid))["state"], "complete")
        self.assertEqual(S3.record(handler.complete_key(pid))["state"], "complete")
        _status, body = call({"action": "resume", "id": pid, "token": token})
        self.assertEqual(body, {"complete": True})


class Notifications(Base):
    def test_a_new_prospect_mails_the_team_its_contact_step(self):
        pid, _, _ = self.started()
        self.assertEqual(len(SES.sent), 1)
        mail = SES.sent[0]
        self.assertEqual(mail["Source"], "Rudi request form <sender@example.org>")
        self.assertEqual(
            mail["Destination"],
            {"ToAddresses": ["team@example.org"], "CcAddresses": ["copy@example.org"]},
        )
        self.assertEqual(
            mail["Message"]["Subject"]["Data"],
            "New prospect started configuration - Alex Example | alex@example.org",
        )
        body = mail["Message"]["Body"]["Html"]["Data"]
        for shown in (
            "Example Health Lab",
            "Programme lead",
            "+32 470 00 00 00",
            "Belgium",
            "Agreed",
            pid,
            handler.partial_key(pid),
        ):
            self.assertIn(shown, body)
        self.assertNotIn("What should Rudi help people do?", body)  # first page only
        self.assertIn(
            "Work e-mail: alex@example.org", mail["Message"]["Body"]["Text"]["Data"]
        )

    def test_bots_drafts_and_middle_steps_send_no_mail(self):
        call(
            {"action": "start", "answers": CONTACT, "elapsed_ms": 30_000, "hp": "spam"}
        )
        self.assertEqual(SES.sent, [])
        pid, token, _ = self.started()
        self.save(
            pid, token, "programme", {"purpose": "Half done, for now."}, draft=True
        )
        self.save(pid, token, "programme", STEPS["programme"])
        self.assertEqual(len(SES.sent), 1)

    def test_a_sent_request_mails_every_answer_in_one_table(self):
        pid, _, _ = self.walk()
        self.assertEqual(len(SES.sent), 2)
        mail = SES.sent[1]
        self.assertEqual(
            mail["Message"]["Subject"]["Data"],
            "New Rudi clone REQUEST COMPLETE - Alex Example | alex@example.org",
        )
        self.assertEqual(mail["Destination"]["CcAddresses"], ["copy@example.org"])
        body = mail["Message"]["Body"]["Html"]["Data"]
        for step in handler.Q["steps"]:
            self.assertIn(step["title"], body)
        for shown in (
            "Clinic or hospital",
            "English, Nederlands",
            "A Rudi number",
            "Read conversations, Take over a conversation",
            "A safety concern, Someone has gone quiet",
            "3 of 5 (Calm to Upbeat)",
            "Azure OpenAI",
            handler.complete_key(pid),
        ):
            self.assertIn(shown, body)
        # Questions that were never shown stay out; open optional ones read "—".
        self.assertNotIn("When may Rudi call?", body)
        self.assertNotIn("Which messages need approval first?", body)
        text = mail["Message"]["Body"]["Text"]["Data"]
        self.assertIn("Something Rudi can’t do yet?: —", text)

    def test_answers_cannot_inject_markup_into_the_mail(self):
        start(organisation="<b>Lab</b> & Co", contact_name="Alex <script>")
        body = SES.sent[0]["Message"]["Body"]["Html"]["Data"]
        self.assertIn("&lt;b&gt;Lab&lt;/b&gt; &amp; Co", body)
        self.assertNotIn("<b>Lab</b>", body)
        self.assertNotIn("<script>", body)

    def test_a_failed_mail_never_fails_the_save(self):
        SES.fail = True
        pid, _, body = self.walk()
        self.assertTrue(body["complete"])
        self.assertEqual(S3.record(handler.complete_key(pid))["state"], "complete")

    def test_no_recipient_means_no_mail(self):
        saved = notify.NOTIFY_TO
        notify.NOTIFY_TO = []
        try:
            status, _ = start()
        finally:
            notify.NOTIFY_TO = saved
        self.assertEqual((status, SES.sent), (201, []))


class Http(Base):
    def test_requests_outside_the_contract_are_refused(self):
        self.assertEqual(call({}, raw="{nope")[0], 400)
        self.assertEqual(call({"action": "delete"})[0], 400)
        self.assertEqual(
            call({}, raw=json.dumps({"action": "open", "pad": "x" * 40_000}))[0], 413
        )
        self.assertEqual(call({}, method="GET")[0], 200)

    def test_records_name_no_person_in_their_key(self):
        _pid, _, _ = self.walk(until="skills")
        for key in S3.objects:
            self.assertRegex(
                key,
                r"^platform/prospects/(partial|complete)/prs_[0-9a-z]{32}\.(partial|complete)\.json$",
            )
            self.assertFalse(re.search(r"alex|example", key))


if __name__ == "__main__":
    unittest.main()
