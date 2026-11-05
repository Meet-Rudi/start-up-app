#!/usr/bin/env python3
"""
MEET_RUDI — play a whole WhatsApp conversation against the DEPLOYED engine, with no phone.

    python services/whatsapp/rudi_sim_chat.py --engine platform --script nl
    python services/whatsapp/rudi_sim_chat.py --engine legacy --script en --out run.json

Each line goes through meetrudi-engine-turn, the Training Arena's invoke-only door, which runs
one turn of the deployed WhatsApp engine exactly as the processor does (de-identify → engine →
restore names) and stores and sends nothing: state and alias vault travel back and forth in
this script. `--engine` picks legacy or the platform Turn Engine (decision #40); without it the
door uses whatever live WhatsApp runs on (CHAT_ENGINE).

Turns run on the arena's OWN Rudi keys (services/training-arena/seed/defaults/config/
cascades.json, "rudi"), never live Rudi's quota: the door refuses anything else. On the arena's
Groq free tier most replies come from its OVH fallback, so expect several seconds a turn.

As on WhatsApp, the language a reply was detected in becomes the next turn's locale. The output
shows each reply, the stage it moved to, the model that answered, the engine that answered and,
when a reply was replaced by the leak guard, why.

Needs the rudi-deployer profile to have lambda:InvokeFunction on meetrudi-engine-turn.
"""

import argparse
import json
import os
import sys
import time

import boto3

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
ACCOUNT_ID = "949753869755"          # MEET_RUDI account; abort if PROFILE resolves anywhere else
DOOR = "meetrudi-engine-turn"
HERE = os.path.dirname(os.path.abspath(__file__))
CASCADES = os.path.join(HERE, "..", "training-arena", "seed", "defaults", "config",
                        "cascades.json")

# What the person writes, line by line. Rudi is a model, so he will not always take the turn a
# script expects; the point is a realistic conversation through every stage, not an exact
# transcript. Names are synthetic.
SCRIPTS = {
    "en": [
        "Hi! I'm Frederik. A friend told me about you.",
        "What exactly do you do?",
        "Sounds good, I'd like to try.",
        "I want to move more. I sit at a desk all day.",
        "Maybe walk after lunch, fifteen minutes.",
        "Yes, every weekday, starting tomorrow.",
        "Sure, check in with me tomorrow evening.",
        "Done! I walked 15 minutes today with my colleague Anna.",
    ],
    "nl": [
        "Hallo! Ik ben Frederik. Een vriend vertelde me over jou.",
        "Wat doe je precies?",
        "Klinkt goed, ik wil het proberen.",
        "Ik wil meer bewegen. Ik zit de hele dag aan mijn bureau.",
        "Misschien na de middag een kwartiertje wandelen.",
        "Ja, elke werkdag, vanaf morgen.",
        "Ja hoor, vraag het me morgenavond maar.",
        "Gelukt! Vandaag 15 minuten gewandeld met mijn collega Anna.",
    ],
    # Starts in English and moves to Dutch: the reply language follows the person.
    "switch": [
        "Hi, I'm Frederik.",
        "Ik spreek eigenlijk liever Nederlands, kan dat?",
        "Ik wil beter slapen.",
        "Om elf uur naar bed, elke avond deze week.",
    ],
}


def _session():
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != ACCOUNT_ID:
        sys.exit(f"!! profile {PROFILE!r} resolves to account {account}, expected {ACCOUNT_ID}"
                 " - refusing to continue.")
    return session.client("lambda")


def arena_cascade():
    """The arena's Rudi cascade, in the door's format."""
    with open(CASCADES, encoding="utf-8") as f:
        rudi = json.load(f)["rudi"]
    return [{"name": e["name"], "kind": e.get("kind") or "openai_compatible",
             "endpoint": e["endpoint"], "model": e["model"], "secret": e["key"]}
            for e in rudi if e.get("on", True)]


def _invoke(lam, event):
    out = lam.invoke(FunctionName=DOOR, Payload=json.dumps(event).encode("utf-8"))
    payload = json.loads(out["Payload"].read() or b"null")
    if out.get("FunctionError"):
        sys.exit(f"!! {DOOR} failed: {json.dumps(payload)[:500]}")
    return payload


def play(lam, engine, lines, locale, persona="", tz="Europe/Brussels"):
    cascade = arena_cascade()
    base = {"tz": tz, "personality": persona,
            "trace": {"case": f"sim-chat-{int(time.time())}"}}
    if engine != "default":
        base["engine"] = engine
    opened = _invoke(lam, {**base, "op": "open", "locale": locale})
    if not opened.get("ok"):
        sys.exit(f"!! open failed: {json.dumps(opened)}")
    print(f"engine asked={engine} answered={opened['engine'].get('name')} "
          f"release={opened['engine'].get('release')}\n")
    print(f"  rudi : {opened['reply']}")
    state, vault = opened["state"], opened["vault"]
    rows = [{"turn": 0, "reply": opened["reply"], "phase": state.get("phase"),
             "engine": opened["engine"].get("name")}]
    for n, text in enumerate(lines, 1):
        started = time.time()
        out = _invoke(lam, {**base, "op": "turn", "text": text, "state": state, "vault": vault,
                            "locale": locale, "cascade": cascade,
                            "trace": {**base["trace"], "turn": n}})
        took = time.time() - started
        print(f"  them : {text}")
        if not out.get("ok"):
            print(f"  !! {out.get('error')}: {str(out.get('detail'))[:200]}")
            rows.append({"turn": n, "error": out.get("error")})
            continue
        info = out["info"]
        meta = (f"[{out['engine'].get('name')} {info.get('phase_before')}->{info.get('phase')} "
                f"{info.get('model')} {took:.1f}s]")
        print(f"  rudi : {out['reply']}  {meta}")
        extra = {k: info[k] for k in ("guard", "halted") if info.get(k)}
        signals = {k: v for k, v in (info.get("signals") or {}).items() if v not in (None, "")}
        if signals or extra:
            print(f"         {json.dumps({**signals, **extra}, ensure_ascii=False)}")
        state, vault = out["state"], out["vault"]
        locale = info.get("lang") or locale        # as the processor does
        rows.append({"turn": n, "text": text, "reply": out["reply"], "engine":
                     out["engine"].get("name"), "phase_before": info.get("phase_before"),
                     "phase": info.get("phase"), "model": info.get("model"), "lang":
                     info.get("lang"), "signals": info.get("signals"), "seconds": round(took, 1),
                     **extra})
    print(f"\nended in stage {state.get('phase')}, goal={state.get('goal')!r}, "
          f"commitment={state.get('commitment')!r}")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Play a WhatsApp conversation against the "
                                                 "deployed engine, with no phone.")
    parser.add_argument("--engine", default="platform", choices=("platform", "legacy", "default"))
    parser.add_argument("--script", default="en", choices=sorted(SCRIPTS))
    parser.add_argument("--persona", default="", help="persona key (default: the clone's)")
    parser.add_argument("--out", help="also write the turns to this JSON file")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")       # Rudi's replies are not cp1252

    lam = _session()
    locale = "nl" if args.script == "nl" else "en"
    rows = play(lam, args.engine, SCRIPTS[args.script], locale, args.persona)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"engine": args.engine, "script": args.script, "turns": rows}, f,
                      ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
