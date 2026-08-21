#!/usr/bin/env python3
"""
MEET_RUDI — place ONE real Dutch test call using an ElevenLabs voice.

    python services/call/rudi_test_call_ElevenLabs_NL.py --num=+32470000000 --greeting

The third sibling of rudi_test_call_Google_NL.py and rudi_test_call_Amazon_NL.py. Same call, same
brain, same gates — only the voice differs.

WHICH VOICE THIS DIALS
    Default: UNBIyLbtFB9k7FKW8wJv, which is Twilio's default ElevenLabs speaker for **nl-NL** —
    Netherlands Dutch, not Flemish. The nl-BE default speaker is s7Z6uboUuE4Nd8Q2nye6, one flag
    away:

        --voice s7Z6uboUuE4Nd8Q2nye6

    Worth hearing both back to back. ElevenLabs voices are multilingual, so the voice ID carries
    the speaker and accent while --language tells the model which language to speak; a
    Netherlands speaker reading Flemish text still reads as Netherlands to the Belgian pilot
    cohort (see services/tts/README.md).

WHY THE DEFAULTS DIFFER FROM WHAT YOU HAVE BEEN HEARING
    Left alone, ConversationRelay serves ElevenLabs at `flash_v2_5` — the latency-optimised, and
    flattest, tier — with text normalization OFF. Those two settings are the leading suspects for
    "the Dutch voice sounds poor", ahead of the voice itself: with normalization off, "18:30"
    and "2,5 kilo" are read as raw characters rather than spoken as Dutch.

    This script therefore defaults to `turbo_v2_5` and normalization ON, which is ElevenLabs at
    its best over a phone line. To hear the current production default for comparison:

        --model flash_v2_5 --normalization off

VOICE TUNING
    Twilio encodes it all into the voice attribute as

        <voice-id>-<model>-<speed>_<stability>_<similarity>

    Ranges: speed 0.7-1.2, stability and similarity 0.0-1.0, all defaulting to 1.0. Lower
    stability is MORE expressive, not less — worth trying 0.5 if the voice sounds flat. The
    script validates the ranges locally, so a typo fails here rather than at dial time.

SAFETY
    The dispatcher's gates still run — consent, active status, quiet hours, destination. This
    script cannot bypass them, and quiet hours are only overridden if you pass
    --override-quiet-hours, which the dispatcher stamps on the manifest and logs as AUDIT.
"""

import os
import re
import sys
import json
import shutil
import argparse
import subprocess
import urllib.error
import urllib.request

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
ACCOUNT_ID = "949753869755"          # MEET_RUDI account; abort if PROFILE resolves anywhere else
STACK = "meetrudi-call"
DISPATCH_SECRET = "meetrudi/call/dispatch-token"

DEFAULT_VOICE = "UNBIyLbtFB9k7FKW8wJv"   # Twilio's nl-NL default speaker
FLEMISH_VOICE = "s7Z6uboUuE4Nd8Q2nye6"   # Twilio's nl-BE default speaker, for --voice
DEFAULT_MODEL = "turbo_v2_5"
MODELS = ("flash_v2_5", "flash_v2", "turbo_v2_5", "turbo_v2")
DEFAULT_LANGUAGE = "nl-BE"
DEFAULT_NAME = "Frederik"
DEFAULT_TOPIC = ("hoe je fitnessoefeningen de afgelopen dagen gingen en wat je "
                 "volgende stap wordt")

# Spoken by Twilio straight from the TwiML the moment the call is answered, BEFORE the brain,
# the socket or the transcriber are involved in anything. That independence is the point: a
# voice audition should not be able to fail because the LLM was slow or Deepgram would not
# connect, which is exactly how the first Google test call died with nothing heard.
#
# The text is chosen to expose what telephone TTS actually gets wrong in Dutch — a name, a
# count, a clock time, a duration, and a decimal comma against a unit. It is also the sharpest
# test of --normalization: with it off, expect the numbers to fall apart.
DEFAULT_GREETING = (
    "Hallo Frederik, dit is Rudi. Je bent de afgelopen dagen 3 keer naar de fitness "
    "geweest, gisteren nog om 18:30, telkens ongeveer 45 minuten. Je tilde 2,5 kilo meer "
    "dan vorige week. Hoe ging het, en wat wordt je volgende stap?"
)

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _aws(*args):
    """Run the AWS CLI, always pinned to PROFILE and REGION.

    Pinned rather than inherited on purpose: this workstation also holds credentials for another
    customer's account, and an unpinned call would succeed there silently.
    """
    cmd = [shutil.which("aws") or "aws", *args, "--region", REGION, "--profile", PROFILE]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def _preflight():
    """Refuse to do anything until the profile really resolves to the MEET_RUDI account."""
    code, out, err = _aws("sts", "get-caller-identity", "--query", "Account", "--output", "text")
    if code != 0 or not out:
        sys.exit("!! cannot resolve an AWS identity for profile %r.\n   %s\n"
                 "   Configure it with:  aws configure --profile %s" % (PROFILE, err, PROFILE))
    if out != ACCOUNT_ID:
        sys.exit("!! profile %r resolves to account %s, expected %s - refusing to continue."
                 % (PROFILE, out, ACCOUNT_ID))


def _dispatch_url():
    code, out, err = _aws("cloudformation", "describe-stacks", "--stack-name", STACK,
                          "--query", "Stacks[0].Outputs[?OutputKey=='DispatchUrl'].OutputValue",
                          "--output", "text")
    if code != 0 or not out:
        sys.exit("!! could not read DispatchUrl from stack %s.\n   %s\n"
                 "   Deploy it first:  python deploy.py call" % (STACK, err))
    return out


def _dispatch_token(explicit=""):
    """Read the shared token. Accepts either shape the secret may have been created in, exactly
    as dispatcher._dispatch_token() does — a plaintext secret is a common console slip.

    Secrets Manager is the intended path, because it keeps the token out of shell history. The
    explicit/env fallback exists because reading that secret needs an IAM statement on
    rudi-deployer that is easy to not have applied yet, and being unable to place a test call is
    a worse outcome than a token in an environment variable.
    """
    token = (explicit or os.environ.get("RUDI_DISPATCH_TOKEN") or "").strip()
    if token:
        return token

    code, out, err = _aws("secretsmanager", "get-secret-value", "--secret-id", DISPATCH_SECRET,
                          "--query", "SecretString", "--output", "text")
    if code != 0 or not out:
        sys.exit("!! could not read %s.\n   %s\n"
                 "   Fix permanently: add infra/iam/rudi-deployer.dispatch-token-add.json to the\n"
                 "   RudiDeployPolicy on the rudi-deployer user (IAM console).\n"
                 "   Or for right now:  set RUDI_DISPATCH_TOKEN=<token from the Secrets Manager "
                 "console>" % (DISPATCH_SECRET, err))
    try:
        value = json.loads(out)
    except ValueError:
        return out.strip()
    return (value.get("token") or "").strip() if isinstance(value, dict) else str(value).strip()


def _post(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"ok": False, "error": raw[:400]}
    except urllib.error.URLError as e:
        sys.exit("!! dispatcher unreachable: %s" % e.reason)


def _dial(value):
    """One decimal minimum, no trailing zero noise: 1.0 -> "1.0", 0.85 -> "0.85".

    Twilio's documented examples are all of the form "1.2_1.0_1.0", so a bare "1" is an untested
    shape in a string that has to be parsed by someone else's code. Not worth finding out.
    """
    text = ("%.2f" % value).rstrip("0")
    return text + "0" if text.endswith(".") else text


def _voice_string(voice_id, model, speed, stability, similarity):
    """Twilio packs the whole ElevenLabs configuration into one attribute. Build it here, and
    validate the ranges, so a bad number is caught now rather than by a call that fails to
    speak."""
    if not 0.7 <= speed <= 1.2:
        sys.exit("!! --speed must be between 0.7 and 1.2 (got %s)" % speed)
    for label, value in (("--stability", stability), ("--similarity", similarity)):
        if not 0.0 <= value <= 1.0:
            sys.exit("!! %s must be between 0.0 and 1.0 (got %s)" % (label, value))
    return "%s-%s-%s_%s_%s" % (voice_id, model, _dial(speed), _dial(stability),
                               _dial(similarity))


def main():
    parser = argparse.ArgumentParser(
        description="Place one Dutch test call with an ElevenLabs voice.")
    parser.add_argument("--num", required=True,
                        help="destination in international format, e.g. --num=+32470000000")
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help="ElevenLabs voice ID (default: %s, Twilio's nl-NL speaker; the "
                             "nl-BE speaker is %s)" % (DEFAULT_VOICE, FLEMISH_VOICE))
    parser.add_argument("--model", default=DEFAULT_MODEL, choices=MODELS,
                        help="ElevenLabs model (default: %s; ConversationRelay's own default is "
                             "flash_v2_5)" % DEFAULT_MODEL)
    parser.add_argument("--speed", type=float, default=1.0, help="0.7-1.2 (default: 1.0)")
    parser.add_argument("--stability", type=float, default=1.0,
                        help="0.0-1.0 (default: 1.0). LOWER is more expressive.")
    parser.add_argument("--similarity", type=float, default=1.0, help="0.0-1.0 (default: 1.0)")
    parser.add_argument("--normalization", default="on", choices=("on", "off"),
                        help="ElevenLabs text normalization, which is what makes '14:30' and "
                             "'1,5 liter' come out as Dutch speech (default: on; "
                             "ConversationRelay's own default is off)")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        help="locale for BOTH the voice and the language Rudi speaks "
                             "(default: %s)" % DEFAULT_LANGUAGE)
    parser.add_argument("--name", default=DEFAULT_NAME, help="who Rudi thinks he is calling")
    parser.add_argument("--topic", default=DEFAULT_TOPIC, help="what the call is about")
    parser.add_argument("--greeting", nargs="?", const=DEFAULT_GREETING, default="",
                        metavar="TEXT",
                        help="speak a fixed line the instant the call is answered, before the "
                             "brain says anything. Bare --greeting uses the built-in Dutch "
                             "sample; pass your own text to override it. Use this to judge the "
                             "voice: it cannot be silenced by a slow model or a failed "
                             "transcriber.")
    parser.add_argument("--override-quiet-hours", action="store_true",
                        help="dial inside 21:30-06:30 Brussels; stamped on the manifest as an "
                             "audited override")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the record and the TwiML, dial nobody")
    parser.add_argument("--token", default="",
                        help="dispatch token, if rudi-deployer may not read it from Secrets "
                             "Manager. RUDI_DISPATCH_TOKEN in the environment does the same and "
                             "keeps it out of shell history.")
    args = parser.parse_args()

    number = args.num.strip().replace(" ", "")
    if not E164.match(number):
        sys.exit("!! --num must be international format starting with '+', e.g. +32470000000")

    voice = _voice_string(args.voice, args.model, args.speed, args.stability, args.similarity)

    _preflight()
    url, token = _dispatch_url(), _dispatch_token(args.token)

    voice_attrs = {
        "ttsProvider": "ElevenLabs",
        "voice": voice,
        "language": args.language,
        "elevenlabsTextNormalization": args.normalization,
    }
    if args.greeting:
        voice_attrs["welcomeGreeting"] = args.greeting
        # Not interruptible: a sample cut off by a cough or a bit of line noise is not a sample.
        voice_attrs["welcomeGreetingInterruptible"] = "none"

    payload = {
        "token": token,
        "dry_run": bool(args.dry_run),
        "config": {
            "to": number,
            "user_name": args.name,
            "topic": args.topic,
            "consent_state": "granted",
            "status": "active",
            "language": args.language,
            "override_quiet_hours": bool(args.override_quiet_hours),
            # The only reason this script exists. Everything else above is an ordinary call.
            "voice_attrs": voice_attrs,
        },
    }

    print("Voice    : ElevenLabs %s (%s)" % (voice, args.language))
    print("Normalize: %s" % args.normalization)
    if args.greeting:
        print("Greeting : %s" % args.greeting)
    print("Calling  : %s%s" % (number, "  [DRY RUN - nobody is dialled]" if args.dry_run else ""))

    status, result = _post(url, payload)

    if result.get("skipped"):
        # A refusal to dial is the safe failure, but a silent one is a confusing failure.
        print("\n!! not dialled - gate: %s" % result["skipped"])
        if result["skipped"] == "quiet-hours":
            print("   It is inside 21:30-06:30 Europe/Brussels. Re-run with "
                  "--override-quiet-hours to dial anyway.")
        sys.exit(1)

    if not result.get("ok"):
        print("\n!! dispatch failed (HTTP %s): %s" % (status, result.get("error")))
        sys.exit(1)

    print("\nOK  call_id: %s" % result.get("call_id"))
    if args.dry_run:
        print("\nTwiML that would have been used:\n%s" % result.get("twiml"))
    else:
        print("    call_sid: %s" % result.get("call_sid"))
        print("    status  : %s" % result.get("status"))
        print("\nThe phone should ring within a few seconds. Transcript lands in S3 under "
              "calls/%s/." % result.get("call_id"))


if __name__ == "__main__":
    main()
