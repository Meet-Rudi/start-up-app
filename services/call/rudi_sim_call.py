#!/usr/bin/env python3
"""
MEET_RUDI — run a whole call against the DEPLOYED call service, with no phone and no Twilio.

    python services/call/rudi_sim_call.py --engine platform --lang nl --goal SET_NEARTERM_GOAL
    python services/call/rudi_sim_call.py --show 20261104-153012-a1b2c3

It does what Twilio does on a real call, by invoking the deployed Lambdas directly:

    meetrudi-call-dispatcher   dry run: the call record is created, nobody is dialled
    meetrudi-call-ws           $connect, `setup` (Rudi's opening), one `prompt` per scripted line,
                               $disconnect

So everything after the phone line is real: the deployed code and layer, the S3 prompts and
knowledge files, the live model cascade and its keys, the time budget. Only speech is missing —
Twilio's transcription and voice. Rudi's replies go to a socket that does not exist, which the
service already treats as "they hung up" for that one frame; what he said is read back from the
turn records in S3, de-identified, exactly as stored.

A simulated call is marked in its config (`placed_by: sim:<engine>`, `simulated: true`) and has
no tester_id, so the tester console never counts it. The same quiet-hours gate as a real call
applies. Nobody is rung, so --override-quiet-hours is safe here; the dispatcher still stamps it on
the call as an audited override.

--show prints any call's transcript, a real phone call's included.

Needs the rudi-deployer profile to have lambda:InvokeFunction on meetrudi-call-* and read access
to the data bucket — no dispatch token, because a direct invoke is not the public URL.
"""

import argparse
import json
import sys
import time

import boto3

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
ACCOUNT_ID = "949753869755"          # MEET_RUDI account; abort if PROFILE resolves anywhere else
BUCKET = "meetrudi-ai-data-%s" % ACCOUNT_ID
DISPATCHER = "meetrudi-call-dispatcher"
WS = "meetrudi-call-ws"
NOBODY = "+3200000000"               # never dialled: a dry run stops before Twilio

LANGUAGES = {"en": "en-GB", "nl": "nl-BE"}   # what the tester console sends
GOAL_START = {"GET_TO_KNOW": "learn", "REINSTATE_TALK": "learn",
              "SET_NEARTERM_GOAL": "goal", "GOAL_FOLLOWUP": "goal"}

TOPIC = {"en": "getting back to daily walking",
         "nl": "terug elke dag gaan wandelen"}
FOLLOWUP_NOTE = {
    "en": "You promised yesterday at 18:00 to call back about: a twenty-minute walk every "
          "morning before work. Ask about that first.",
    "nl": "You promised yesterday at 18:00 to call back about: elke ochtend twintig minuten "
          "wandelen voor het werk. Ask about that first.",
}

# What the person says, line by line. Rudi is a model, so he will not always take the turn a
# script expects; the point is a realistic call through every stage, not an exact transcript.
SCRIPTS = {
    ("en", "learn"): [
        "Hello?",
        "Yes, that's me. Sorry, what exactly are you?",
        "Okay. I'd like to sleep better, I'm tired all the time.",
        "Yes, I'd like to try that.",
        "Sure, I'll send you a WhatsApp message later today.",
        "Okay, thanks. Bye!",
    ],
    ("en", "goal"): [
        "Hi, yes, speaking.",
        "Yes, that's right. I want to walk more, maybe twenty minutes a day.",
        "Mornings work best, before work.",
        "Yes, tomorrow morning, twenty minutes around the block.",
        "Yes, I promise. You can call me tomorrow evening to check.",
        "Thanks, bye!",
    ],
    ("nl", "learn"): [
        "Hallo?",
        "Ja, dat ben ik. Sorry, wat ben jij eigenlijk?",
        "Oké. Ik zou beter willen slapen, ik ben altijd moe.",
        "Ja, dat wil ik wel proberen.",
        "Ja hoor, ik stuur je straks een WhatsAppje.",
        "Oké, merci. Dag!",
    ],
    ("nl", "goal"): [
        "Hallo, ja, met mij.",
        "Ja, dat klopt. Ik wil meer wandelen, een twintigtal minuten per dag.",
        "'s Morgens lukt het best, voor het werk.",
        "Ja, morgenvroeg, twintig minuten rond de blok.",
        "Ja, beloofd. Bel me morgenavond maar om te checken.",
        "Merci, dag!",
    ],
}
FOLLOWUP_SCRIPT = {
    "en": ["Hello?",
           "Yes. I did it twice, but this morning it was raining, so I skipped it.",
           "Maybe I could walk at lunchtime instead when it rains.",
           "Yes, I'll do that tomorrow.",
           "Okay, bye!"],
    "nl": ["Hallo?",
           "Ja. Twee keer gedaan, maar vanochtend regende het, dus heb ik het overgeslagen.",
           "Misschien kan ik 's middags gaan wandelen als het regent.",
           "Ja, dat doe ik morgen.",
           "Oké, dag!"],
}


def _session():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != ACCOUNT_ID:
        sys.exit("!! profile %r resolves to account %s, expected %s - refusing to continue."
                 % (PROFILE, account, ACCOUNT_ID))
    return session.client("lambda"), session.client("s3")


def _invoke(lam, name, event):
    out = lam.invoke(FunctionName=name, Payload=json.dumps(event).encode("utf-8"))
    payload = json.loads(out["Payload"].read() or b"null")
    if out.get("FunctionError"):
        sys.exit("!! %s failed: %s" % (name, json.dumps(payload)[:500]))
    return payload


def _get(s3, key):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def _frame(connection, route, body=None):
    event = {"requestContext": {"routeKey": route, "connectionId": connection}}
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _line(record):
    if record is None:
        return "    (no turn record)"
    out = []
    if record.get("transcript"):
        out.append("  them : %s" % record["transcript"])
    reply = record.get("reply")
    meta = "[%s %s %s %sms]" % (record.get("engine", "legacy"), record.get("phase"),
                                record.get("model"),
                                (record.get("timings") or {}).get("llm_ms"))
    out.append("  rudi : %s  %s" % (reply if reply else "(%s)" % record.get("kind"), meta))
    signals = {k: v for k, v in (record.get("signals") or {}).items() if v not in (None, "", {})}
    if signals:
        out.append("         signals %s" % json.dumps(signals, ensure_ascii=False))
    return "\n".join(out)


def show(s3, call_id):
    manifest = _get(s3, "calls/%s/manifest.json" % call_id)
    if not manifest:
        sys.exit("!! no call %s" % call_id)
    config = manifest.get("config") or {}
    tel = manifest.get("telephony") or {}
    print("call %s  engine=%s  lang=%s  goal=%s  voice=%s"
          % (call_id, manifest.get("engine", "legacy"), config.get("language"),
             config.get("call_goal"), tel.get("voice")))
    for seq in range(0, int((manifest.get("totals") or {}).get("turns") or 0) + 1):
        print(_line(_get(s3, "calls/%s/turns/%04d.json" % (call_id, seq))))
    print("ended: %s  outcome: %s  pii: %s%s"
          % (manifest.get("end_reason"), json.dumps(manifest.get("outcome"), ensure_ascii=False),
             json.dumps((manifest.get("pii") or {}).get("redacted") or {}),
             "  FALLBACK: %s" % manifest["engine_fallback"] if manifest.get("engine_fallback")
             else ""))
    return manifest


def simulate(lam, s3, args):
    lang = args.lang
    start = GOAL_START[args.goal]
    script = (FOLLOWUP_SCRIPT[lang] if args.goal == "GOAL_FOLLOWUP"
              else SCRIPTS[(lang, start)])
    config = {
        "to": NOBODY,
        "user_name": args.name,
        "language": LANGUAGES[lang],
        "timezone": "Europe/Brussels",
        "consent_state": "granted",
        "status": "active",
        "topic": TOPIC[lang],
        "call_goal": args.goal,
        "start_phase": start,
        "placed_by": "sim:%s" % args.engine,
        "simulated": True,
    }
    if args.engine != "default":          # "default": whatever CALL_ENGINE says, as testers get
        config["engine"] = args.engine
    if args.goal == "GOAL_FOLLOWUP":
        config["notes"] = FOLLOWUP_NOTE[lang]
    if args.models:
        config["models"] = args.models
    if args.override_quiet_hours:
        config["override_quiet_hours"] = True

    response = _invoke(lam, DISPATCHER, {"dry_run": True, "config": config})
    placed = json.loads(response.get("body") or "{}")
    if not placed.get("ok"):
        sys.exit("!! not placed: %s" % json.dumps(placed))
    call_id = placed["call_id"]
    connection = "sim-%s" % call_id
    print("call %s  engine=%s  lang=%s  goal=%s\n" % (call_id, args.engine, config["language"],
                                                     args.goal))

    _invoke(lam, WS, _frame(connection, "$connect"))
    started = time.time()
    _invoke(lam, WS, _frame(connection, "$default", {
        "type": "setup", "sessionId": "sim", "callSid": "sim-" + call_id, "from": NOBODY,
        "direction": "outbound-api", "customParameters": {"call_id": call_id}}))
    print("  (opening took %.1fs)" % (time.time() - started))
    print(_line(_get(s3, "calls/%s/turns/0000.json" % call_id)))

    seq = 0
    for said in script:
        manifest = _get(s3, "calls/%s/manifest.json" % call_id) or {}
        if manifest.get("status") == "completed":
            break
        seq += 1
        started = time.time()
        _invoke(lam, WS, _frame(connection, "$default",
                                {"type": "prompt", "voicePrompt": said, "last": True}))
        took = time.time() - started
        print(_line(_get(s3, "calls/%s/turns/%04d.json" % (call_id, seq))))
        print("         (%.1fs end to end)" % took)

    _invoke(lam, WS, _frame(connection, "$disconnect"))
    print()
    return show(s3, call_id)


def main():
    parser = argparse.ArgumentParser(description="Run a whole call against the deployed call "
                                                 "service, with no phone.")
    parser.add_argument("--engine", default="platform", choices=("platform", "legacy", "default"),
                        help="'default' leaves it to the service's CALL_ENGINE, as a tester's "
                             "call does")
    parser.add_argument("--lang", default="en", choices=sorted(LANGUAGES))
    parser.add_argument("--goal", default="SET_NEARTERM_GOAL", choices=sorted(GOAL_START))
    parser.add_argument("--name", default="Frederik", help="who Rudi thinks he is calling")
    parser.add_argument("--models", nargs="+", metavar="ENDPOINT",
                        help="platform engine only: pin the call to these endpoints of "
                             "config/ai_endpoints.json, to time one provider on a call")
    parser.add_argument("--override-quiet-hours", action="store_true",
                        help="simulate inside 21:30-06:30 Brussels (nobody is rung; audited)")
    parser.add_argument("--show", metavar="CALL_ID", help="print a call's transcript and stop")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")       # Rudi's replies are not cp1252

    lam, s3 = _session()
    if args.show:
        show(s3, args.show)
    else:
        simulate(lam, s3, args)


if __name__ == "__main__":
    main()
