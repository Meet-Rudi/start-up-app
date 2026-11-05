#!/usr/bin/env python3
"""
MEET_RUDI — place ONE real test call on the platform engine, in a chosen language and voice.

    python services/call/rudi_test_call_platform.py --plan
    python services/call/rudi_test_call_platform.py --step 1
    python services/call/rudi_test_call_platform.py --num=+32470000000 --lang en --voice ppGIZI01uUlIWI734dUU
    python services/call/rudi_test_call_platform.py --list-voices

THE PLAN
    `--step N` places call N of PLAN below: both languages, the production voices and several
    ElevenLabs voices from CM's voice audition (one of them speaking English), and every call
    goal. The number dialled is --num, else RUDI_TEST_NUMBER from the environment.

The call is the one the tester console places (same config shape, same dispatcher, same gates),
with one difference: its config asks for `engine: platform`, so it runs on CM's clone on the
platform Turn Engine whatever the service default is (services/call/src/platform_call.py).
`--engine legacy` places the same call on brain.py, for a side-by-side.

VOICE
    No --voice: the production voice for the language, exactly what a tester hears
    (relay.VOICE_PROFILES: Dutch = ElevenLabs Luk Belcer, English = Google en-US-Journey-D).
    --voice <ElevenLabs id>: that voice, set up as in the August voice audition
    (turbo_v2_5, 1.0_1.0_1.0, text normalization on). ElevenLabs voices are multilingual, so any
    of them can be paired with either language.
    --voice google:<name> / amazon:<name>: another provider's voice.

Then read what was said:  python services/call/rudi_sim_call.py --show <call_id>

Same safety as the other test scripts: the dispatcher's gates still run, and quiet hours are only
overridden with --override-quiet-hours, which the dispatcher stamps on the call as AUDIT.
"""

import argparse
import os
import sys

from rudi_test_call_ElevenLabs_NL import (
    E164,
    _dispatch_token,
    _dispatch_url,
    _post,
    _preflight,
    _voice_string,
)

LANGUAGES = {"en": "en-GB", "nl": "nl-BE"}   # what the tester console sends
GOAL_START = {"GET_TO_KNOW": "learn", "REINSTATE_TALK": "learn",
              "SET_NEARTERM_GOAL": "goal", "GOAL_FOLLOWUP": "goal"}
TOPIC = {"en": "getting back to daily walking", "nl": "terug elke dag gaan wandelen"}
FOLLOWUP_NOTE = {
    "en": "You promised yesterday at 18:00 to call back about: a twenty-minute walk every "
          "morning before work. Ask about that first.",
    "nl": "You promised yesterday at 18:00 to call back about: elke ochtend twintig minuten "
          "wandelen voor het werk. Ask about that first.",
}

# Every voice CM testing has put on a call (call records, August-September 2026).
PRODUCTION = {"nl": "ElevenLabs ppGIZI01uUlIWI734dUU (Luk Belcer, pinned)",
              "en": "Google en-US-Journey-D"}
ELEVENLABS_AUDITIONED = (
    "ppGIZI01uUlIWI734dUU",   # Luk Belcer — the production Dutch voice
    "s7Z6uboUuE4Nd8Q2nye6",   # Twilio's nl-BE default speaker
    "UNBIyLbtFB9k7FKW8wJv",   # Twilio's nl-NL default speaker
    "6mBnRhL0dHfQ4ypWLcIi",
    "ANHrhmaFeVN0QJaa0PhL",
    "tRyB8BgRzpNUv3o2XWD4",
    "wqDY19Brqhu7UCoLadPh",
    "4SZMuFG3NOs5lWy1q5Wf",
    "9kBSa5emtWArU7U0792v",
    "Otc3wCRPKNSpn8EDImmM",
    "PrYUlaJFEdOSVy6jaEaG",
    "Q6dV0r9H1dI4d6gz3NVD",
)
OTHER_AUDITIONED = ("google:nl-BE-Wavenet-C", "amazon:Lisa-Neural")

# The phone test of CM's calls on the platform engine (decision #37): each language on its
# production voice, then auditioned ElevenLabs voices in both languages, across all call goals.
PLAN = (
    ("nl", "", "SET_NEARTERM_GOAL", "Dutch, production voice (Luk Belcer)"),
    ("en", "", "SET_NEARTERM_GOAL", "English, production voice (Google Journey-D)"),
    ("nl", "", "GET_TO_KNOW", "Dutch first call: learn stage, WhatsApp hand-over"),
    ("en", "ppGIZI01uUlIWI734dUU", "GET_TO_KNOW", "Luk Belcer speaking ENGLISH"),
    ("nl", "s7Z6uboUuE4Nd8Q2nye6", "GOAL_FOLLOWUP", "Dutch on Twilio's nl-BE ElevenLabs voice"),
    ("en", "6mBnRhL0dHfQ4ypWLcIi", "GOAL_FOLLOWUP", "English on an auditioned ElevenLabs voice"),
    ("nl", "UNBIyLbtFB9k7FKW8wJv", "SET_NEARTERM_GOAL", "Dutch on the nl-NL ElevenLabs voice"),
)


def _voice_attrs(voice, language):
    """The TwiML override for --voice, or None for the production voice."""
    if not voice:
        return None
    provider, _, name = voice.partition(":")
    if name:
        providers = {"google": "Google", "amazon": "Amazon", "elevenlabs": "ElevenLabs"}
        if provider.lower() not in providers:
            sys.exit("!! unknown provider %r (google, amazon, elevenlabs)" % provider)
        provider, voice = providers[provider.lower()], name
    else:
        provider = "ElevenLabs"
    attrs = {"ttsProvider": provider, "language": language}
    if provider == "ElevenLabs":
        attrs["voice"] = _voice_string(voice, "turbo_v2_5", 1.0, 1.0, 1.0)
        attrs["elevenlabsTextNormalization"] = "on"
    else:
        attrs["voice"] = voice
    return attrs


def main():
    parser = argparse.ArgumentParser(description="Place one test call on the platform engine.")
    parser.add_argument("--num", default=os.environ.get("RUDI_TEST_NUMBER", ""),
                        help="destination in international format, e.g. +32470000000 "
                             "(default: RUDI_TEST_NUMBER)")
    parser.add_argument("--plan", action="store_true", help="print the test plan, then stop")
    parser.add_argument("--step", type=int, choices=range(1, len(PLAN) + 1), metavar="N",
                        help="place call N of the plan (sets --lang, --voice and --goal)")
    parser.add_argument("--lang", default="nl", choices=sorted(LANGUAGES))
    parser.add_argument("--voice", default="",
                        help="ElevenLabs voice id, or google:<name> / amazon:<name> "
                             "(default: the production voice for --lang)")
    parser.add_argument("--goal", default="SET_NEARTERM_GOAL", choices=sorted(GOAL_START),
                        help="the call goal, as the tester console sets it")
    parser.add_argument("--engine", default="platform", choices=("platform", "legacy"))
    parser.add_argument("--name", default="Frederik", help="who Rudi thinks he is calling")
    parser.add_argument("--topic", default="", help="what the call is about")
    parser.add_argument("--override-quiet-hours", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the record and the TwiML, dial nobody")
    parser.add_argument("--token", default="",
                        help="dispatch token, if rudi-deployer may not read it "
                             "(RUDI_DISPATCH_TOKEN in the environment does the same)")
    parser.add_argument("--list-voices", action="store_true",
                        help="print the voices CM testing has used, then stop")
    args = parser.parse_args()

    if args.plan:
        for n, (lang, voice, goal, what) in enumerate(PLAN, 1):
            print("  --step %d  %-2s  %-20s  %-17s  %s"
                  % (n, lang, voice or "production", goal, what))
        return
    if args.step:
        args.lang, args.voice, args.goal, what = PLAN[args.step - 1]
        print("Step %d of %d: %s" % (args.step, len(PLAN), what))

    if args.list_voices:
        print("Production (no --voice):")
        for lang, voice in sorted(PRODUCTION.items()):
            print("  --lang %s  %s" % (lang, voice))
        print("ElevenLabs, as auditioned (--voice <id>, either language):")
        for voice in ELEVENLABS_AUDITIONED:
            print("  %s" % voice)
        print("Other providers (Dutch only):")
        for voice in OTHER_AUDITIONED:
            print("  %s" % voice)
        return

    number = (args.num or "").strip().replace(" ", "")
    if not E164.match(number):
        sys.exit("!! the number to call (--num, or RUDI_TEST_NUMBER) must be international "
                 "format starting with '+', e.g. +32470000000")

    language = LANGUAGES[args.lang]
    config = {
        "to": number,
        "user_name": args.name,
        "language": language,
        "timezone": "Europe/Brussels",
        "consent_state": "granted",
        "status": "active",
        "topic": args.topic or TOPIC[args.lang],
        "call_goal": args.goal,
        "start_phase": GOAL_START[args.goal],
        "engine": args.engine,
        "placed_by": "test:%s" % args.engine,
        "override_quiet_hours": bool(args.override_quiet_hours),
    }
    if args.goal == "GOAL_FOLLOWUP":
        config["notes"] = FOLLOWUP_NOTE[args.lang]
    attrs = _voice_attrs(args.voice, language)
    if attrs:
        config["voice_attrs"] = attrs

    _preflight()
    url, token = _dispatch_url(), _dispatch_token(args.token)

    print("Engine   : %s" % args.engine)
    print("Language : %s" % language)
    print("Voice    : %s" % ("%s %s" % (attrs["ttsProvider"], attrs["voice"]) if attrs
                             else "production (%s)" % PRODUCTION[args.lang]))
    print("Goal     : %s (starts in %s)" % (args.goal, GOAL_START[args.goal]))
    print("Calling  : %s%s" % (number, "  [DRY RUN - nobody is dialled]" if args.dry_run else ""))

    status, result = _post(url, {"token": token, "dry_run": bool(args.dry_run),
                                 "config": config})
    if result.get("skipped"):
        print("\n!! not dialled - gate: %s" % result["skipped"])
        if result["skipped"] == "quiet-hours":
            print("   It is inside 21:30-06:30 Europe/Brussels. Re-run with "
                  "--override-quiet-hours to dial anyway.")
        sys.exit(1)
    if not result.get("ok"):
        print("\n!! dispatch failed (HTTP %s): %s" % (status, result.get("error")))
        sys.exit(1)

    call_id = result.get("call_id")
    print("\nOK  call_id: %s" % call_id)
    if args.dry_run:
        print("\nTwiML that would have been used:\n%s" % result.get("twiml"))
    else:
        print("    The phone should ring within a few seconds.")
        print("    Afterwards:  python services/call/rudi_sim_call.py --show %s" % call_id)


if __name__ == "__main__":
    main()
