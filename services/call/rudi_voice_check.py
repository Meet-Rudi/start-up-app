#!/usr/bin/env python3
"""
MEET_RUDI — check that each call voice really speaks its language. Real calls, nobody on the line.

    python services/call/rudi_voice_check.py --setup        once: the check line answers
    python services/call/rudi_voice_check.py                 the plan below, one call at a time
    python services/call/rudi_voice_check.py --all           every auditioned voice, both languages
    python services/call/rudi_voice_check.py --teardown      the check line stops answering

Each check is a real Twilio call (services/call/src/voicecheck.py) from Rudi's number to the
account's own spare number, which picks up and stays silent. Rudi speaks a fixed sample sentence
in the call's language (ConversationRelay's welcome greeting, in the voice under test), then his
normal opening from the platform engine. Twilio records the call; the dispatcher refuses to record
anything but the account's own number. Whisper then transcribes Rudi's side without being told the
language, and a model judges it against what Rudi was given to say.

Out: a line per voice here, calls/{id}/voice_check.json in S3, and Rudi's side as a WAV under
_voice_samples/voice-check/ (git-ignored) to listen to.

Uses direct Lambda invokes as rudi-deployer: no dispatch token. Calls only our own line, so quiet
hours are overridden (audited on each call).
"""

import argparse
import json
import os
import re
import sys
import time

import boto3

from rudi_test_call_platform import ELEVENLABS_AUDITIONED, _voice_attrs

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
ACCOUNT_ID = "949753869755"
BUCKET = "meetrudi-ai-data-%s" % ACCOUNT_ID
DISPATCHER = "meetrudi-call-dispatcher"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(ROOT, "_voice_samples", "voice-check")

LANGUAGES = {"en": "en-GB", "nl": "nl-BE"}     # what the tester console sends
SAMPLE = {
    "nl": ("Hallo, met Rudi. Dit is een korte test van mijn stem. Je wandelde gisteren twintig "
           "minuten, om half acht, en dat is heel goed gedaan."),
    "en": ("Hello, this is Rudi. This is a short test of my voice. You walked for twenty minutes "
           "yesterday, at half past seven, and that is very well done."),
}
EXPECTED = {"en": "english", "nl": "dutch"}    # Whisper's names

# Production voices first (what testers hear), then voices from CM's August audition in both
# languages — ElevenLabs voices are multilingual, so the same voice must switch language cleanly.
PLAN = [
    ("nl", ""), ("en", ""),
    ("en", "ppGIZI01uUlIWI734dUU"),
    ("nl", "s7Z6uboUuE4Nd8Q2nye6"), ("en", "s7Z6uboUuE4Nd8Q2nye6"),
    ("nl", "UNBIyLbtFB9k7FKW8wJv"), ("en", "UNBIyLbtFB9k7FKW8wJv"),
    ("nl", "6mBnRhL0dHfQ4ypWLcIi"), ("en", "6mBnRhL0dHfQ4ypWLcIi"),
    ("nl", "google:nl-BE-Wavenet-C"), ("nl", "amazon:Lisa-Neural"),
]


def _clients():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != ACCOUNT_ID:
        sys.exit("!! profile %r resolves to account %s, expected %s" % (PROFILE, account,
                                                                        ACCOUNT_ID))
    return session.client("lambda"), session.client("s3")


def _dispatch(lam, payload):
    out = lam.invoke(FunctionName=DISPATCHER, Payload=json.dumps(payload).encode("utf-8"))
    body = json.loads(out["Payload"].read() or b"null")
    if out.get("FunctionError"):
        sys.exit("!! dispatcher failed: %s" % json.dumps(body)[:400])
    return json.loads(body.get("body") or "{}")


def _check_line(lam):
    """The own number that answers checks: a voice number that is not the caller id."""
    listed = _dispatch(lam, {"action": "numbers"})
    for n in listed.get("numbers", []):
        if n["phone_number"] != listed.get("from_number") and (n.get("capabilities") or {}).get(
                "voice"):
            return n
    sys.exit("!! the Twilio account has no spare voice number to act as the check line")


def _masked(number):
    return "%s…%s" % (number[:3], number[-4:])


def check(lam, s3, line, lang, voice):
    language = LANGUAGES[lang]
    attrs = _voice_attrs(voice, language) or {}
    attrs.update({"welcomeGreeting": SAMPLE[lang], "welcomeGreetingInterruptible": "none"})
    config = {
        "to": line, "voice_check": True, "engine": "platform",
        "user_name": "Frederik", "language": language, "timezone": "Europe/Brussels",
        "consent_state": "granted", "status": "active",
        "topic": "terug elke dag gaan wandelen" if lang == "nl" else "getting back to daily walking",
        "call_goal": "SET_NEARTERM_GOAL", "start_phase": "goal",
        "voice_attrs": attrs, "override_quiet_hours": True, "placed_by": "voice-check",
    }
    placed = _dispatch(lam, {"config": config})
    if not placed.get("ok"):
        return {"ok": False, "error": placed.get("skipped") or placed.get("error")}
    call_id = placed["call_id"]
    key = "calls/%s/audio/recording.wav" % call_id
    deadline = time.time() + 150
    while time.time() < deadline:
        time.sleep(8)
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            break
        except s3.exceptions.ClientError:
            continue
    else:
        return {"ok": False, "call_id": call_id, "error": "no recording after 150 s"}
    report = _dispatch(lam, {"action": "voice_check_report", "call_id": call_id})
    if report.get("ok"):
        os.makedirs(OUT_DIR, exist_ok=True)
        name = "%s-%s-%s.wav" % (lang, re.sub(r"[^A-Za-z0-9]+", "-", voice or "production"),
                                 call_id)
        s3.download_file(BUCKET, report["rudi_audio"], os.path.join(OUT_DIR, name))
        report["file"] = os.path.join(OUT_DIR, name)
    return report


def main():
    parser = argparse.ArgumentParser(description="Check each call voice speaks its language.")
    parser.add_argument("--setup", action="store_true", help="point the check line at us")
    parser.add_argument("--teardown", action="store_true", help="the check line stops answering")
    parser.add_argument("--all", action="store_true",
                        help="every auditioned ElevenLabs voice in both languages, plus the rest")
    parser.add_argument("--only", type=int, nargs="+", metavar="N", help="plan steps to run")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    lam, s3 = _clients()
    if args.setup or args.teardown:
        out = _dispatch(lam, {"action": "voice_check_line", "enable": bool(args.setup)})
        if not out.get("ok"):
            sys.exit("!! %s" % out.get("error"))
        print("check line %s -> %s" % (_masked(out["number"]), out["voice_url"] or "(cleared)"))
        return

    plan = list(PLAN)
    if args.all:
        plan += [(lang, v) for v in ELEVENLABS_AUDITIONED for lang in ("nl", "en")
                 if (lang, v) not in plan]
    if args.only:
        plan = [plan[n - 1] for n in args.only if 0 < n <= len(plan)]
    line = _check_line(lam)
    print("check line %s (%s); %d voice checks\n" % (_masked(line["phone_number"]),
                                                     "answers" if line.get("voice_url")
                                                     else "NOT SET UP: run --setup", len(plan)))
    results = []
    for n, (lang, voice) in enumerate(plan, 1):
        r = check(lam, s3, line["phone_number"], lang, voice)
        verdict = r.get("verdict") or {}
        passed = (r.get("ok") and r.get("heard_language") == EXPECTED[lang]
                  and verdict.get("language_ok") is True)
        results.append(dict(r, lang=lang, voice_asked=voice or "production", passed=bool(passed)))
        print("%2d %s  %-2s  %-24s heard=%-8s judge=%s  %s" % (
            n, "PASS" if passed else "FAIL", lang, voice or "production",
            r.get("heard_language") or "-",
            "lang %s text %s" % (verdict.get("language_ok"), verdict.get("text_ok"))
            if verdict else r.get("error"), r.get("call_id", "")))
        if r.get("heard"):
            print("      heard: %s" % r["heard"][:160])
        if verdict.get("accent") or verdict.get("notes"):
            print("      judge: %s %s" % (verdict.get("accent") or "", verdict.get("notes") or ""))
    os.makedirs(OUT_DIR, exist_ok=True)
    summary = os.path.join(OUT_DIR, "summary-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(summary, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n%d of %d passed. Audio and summary: %s" % (sum(r["passed"] for r in results),
                                                         len(results), OUT_DIR))


if __name__ == "__main__":
    main()
