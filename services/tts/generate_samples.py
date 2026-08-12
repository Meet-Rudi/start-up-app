#!/usr/bin/env python3
"""
Render the Rudi coaching monologue through every candidate voice, for A/B listening.

    python services/tts/generate_samples.py

Writes WAVs to _voice_samples/ in the repo root (git-ignored) plus an index.html that lets you
play them back to back. English text goes through the English voices, Flemish text through the
Dutch ones — the point is to judge each voice on the language it would actually speak.

WAV, not MP3, on purpose: this exists to assess voice quality, and listening through a lossy
encoder would put an artefact between you and the thing you are judging.

The script calls the deployed meetrudi-tts Function URL, so it measures the real path — cold
starts, S3 voice pulls and all. It reads the shared token from Secrets Manager with the
rudi-deployer profile; pass --token to override.
"""

import os
import sys
import json
import time
import base64
import subprocess
import urllib.request

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
STACK = "meetrudi-tts"
SECRET_ID = "meetrudi/tts/token"

# Silence inserted after every sentence. Piper leaves none at all, which is why full stops used
# to run straight into the next sentence. Override with --gap=NNN to audition a different beat.
SENTENCE_GAP_MS = 300
for _a in sys.argv[1:]:
    if _a.startswith("--gap="):
        SENTENCE_GAP_MS = int(_a.split("=", 1)[1])

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "_voice_samples")

TEXT_EN = (
    "Filip, I have to tell you — I've been looking at your last three weeks, and I am genuinely "
    "impressed. Four runs a week. Every single week. Do you know how rare that is? "
    "Here's what I noticed, though. You've been running the same twenty minutes since day one. "
    "Your body has already caught up with that. It's comfortable now. And comfortable is where "
    "progress quietly stops. "
    "So I want to ask you for something small. Not double. Not heroic. Just five more minutes on "
    "two of your runs this week. Twenty-five instead of twenty. That's it. "
    "If that feels good next week, we add five more. Slowly. Boringly. That's how this actually "
    "works. "
    "And listen — if anything starts to hurt, if your knee complains, you stop and you talk to "
    "your doctor before you talk to me. I'm here for the habit, not the medicine. "
    "But the habit? The habit you've already built. Now we just give it a little more room. "
    "So — twenty-five minutes, twice this week. Are you in?"
)

TEXT_NL = (
    "Filip, ik moet het je echt even zeggen — ik heb naar je laatste drie weken gekeken, en ik "
    "ben oprecht onder de indruk. Vier keer lopen per week. Elke week opnieuw. Weet je hoe "
    "zeldzaam dat is? "
    "Maar er is iets dat me opvalt. Je loopt nog altijd dezelfde twintig minuten als op dag één. "
    "Je lichaam is daar ondertussen aan gewend. Het voelt comfortabel. En comfortabel is net "
    "waar vooruitgang stilletjes stopt. "
    "Dus ik wil je iets kleins vragen. Niet het dubbele. Niets heldhaftigs. Gewoon vijf minuten "
    "extra, bij twee van je loopjes deze week. Vijfentwintig in plaats van twintig. Meer niet. "
    "Als dat volgende week goed voelt, doen we er weer vijf bij. Traag. Saai. Zo werkt het nu "
    "eenmaal echt. "
    "En luister — als er iets pijn begint te doen, als je knie protesteert, dan stop je en praat "
    "je met je arts, nog voor je met mij praat. Ik ben er voor de gewoonte, niet voor de "
    "geneeskunde. "
    "Maar die gewoonte? Die heb je al opgebouwd. Nu geven we ze alleen wat meer ruimte. "
    "Dus — vijfentwintig minuten, twee keer deze week. Doe je mee?"
)

# The same monologue with deliberate beats written in, to hear what [pause:N] buys. Placed at
# the rhetorical turns — after the compliment lands, before the ask, around the medical caveat.
BEATS_EN = (
    "Filip, I have to tell you —[pause:350] I've been looking at your last three weeks, and I am "
    "genuinely impressed.[pause:600] Four runs a week.[pause:300] Every single week.[pause:400] "
    "Do you know how rare that is?[pause:800] "
    "Here's what I noticed, though.[pause:400] You've been running the same twenty minutes since "
    "day one. Your body has already caught up with that. It's comfortable now.[pause:450] And "
    "comfortable is where progress quietly stops.[pause:700] "
    "So I want to ask you for something small.[pause:350] Not double. Not heroic.[pause:400] "
    "Just five more minutes on two of your runs this week. Twenty-five instead of twenty."
    "[pause:400] That's it.[pause:700] "
    "If that feels good next week, we add five more. Slowly.[pause:250] Boringly.[pause:350] "
    "That's how this actually works.[pause:700] "
    "And listen —[pause:300] if anything starts to hurt, if your knee complains, you stop and you "
    "talk to your doctor before you talk to me.[pause:450] I'm here for the habit, not the "
    "medicine.[pause:800] "
    "But the habit?[pause:400] The habit you've already built.[pause:400] Now we just give it a "
    "little more room.[pause:800] "
    "So —[pause:300] twenty-five minutes, twice this week.[pause:500] Are you in?"
)

# The full voice comparison, all at the voice's natural pace.
SAMPLES = [
    {"voice": "nl_BE-nathalie-medium", "lang": "nl", "label": "Flemish · female"},
    {"voice": "nl_BE-rdh-medium", "lang": "nl", "label": "Flemish · male"},
    {"voice": "nl_NL-alex-medium", "lang": "nl", "label": "Netherlands · male"},
    {"voice": "nl_NL-pim-medium", "lang": "nl", "label": "Netherlands · male"},
    {"voice": "nl_NL-ronnie-medium", "lang": "nl", "label": "Netherlands · male"},
    {"voice": "nl_NL-mls-medium", "lang": "nl", "label": "Netherlands · multi-speaker (0)",
     "speaker_id": 0},
    {"voice": "en_US-lessac-medium", "lang": "en", "label": "US · female"},
    {"voice": "en_US-amy-medium", "lang": "en", "label": "US · female"},
    {"voice": "en_US-ryan-high", "lang": "en", "label": "US · male · high quality"},
    {"voice": "en_GB-alan-medium", "lang": "en", "label": "UK · male"},
    {"voice": "en_GB-jenny_dioco-medium", "lang": "en", "label": "UK · female"},

    # Tempo ladder on the two likely finalists. length_scale > 1 slows the voice down; every
    # Piper voice ships at 1.0, so pace differences between voices are baked into the model
    # and this is the only lever that evens them out.
    {"voice": "nl_BE-nathalie-medium", "lang": "nl", "label": "Flemish · 15% slower",
     "length_scale": 1.15, "suffix": "ls115"},
    {"voice": "nl_BE-nathalie-medium", "lang": "nl", "label": "Flemish · 30% slower",
     "length_scale": 1.30, "suffix": "ls130"},
    {"voice": "en_US-lessac-medium", "lang": "en", "label": "US · 15% slower",
     "length_scale": 1.15, "suffix": "ls115"},
    {"voice": "en_US-lessac-medium", "lang": "en", "label": "US · 30% slower",
     "length_scale": 1.30, "suffix": "ls130"},

    # Same voice, same pace, deliberate beats written into the text.
    {"voice": "en_US-lessac-medium", "lang": "en", "label": "US · with deliberate beats",
     "text": BEATS_EN, "suffix": "beats"},
]


def _capture(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _stack_output(key):
    out = _capture(["aws", "cloudformation", "describe-stacks", "--stack-name", STACK,
                    "--region", REGION, "--profile", PROFILE,
                    "--query", "Stacks[0].Outputs[?OutputKey=='%s'].OutputValue" % key,
                    "--output", "text"])
    return (out.stdout or "").strip()


TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def _unwrap(raw):
    raw = (raw or "").strip()
    try:
        return str(json.loads(raw).get("token", raw)).strip()
    except (ValueError, TypeError, AttributeError):
        return raw


def _token():
    """--token= > MEETRUDI_TTS_TOKEN > services/tts/.token > Secrets Manager.

    rudi-deployer is not granted secretsmanager:GetSecretValue (only the Lambda runner role is),
    so the local file is the practical path: write the token there once and it stays out of
    shell history, out of the transcript, and out of git.
    """
    for arg in sys.argv[1:]:
        if arg.startswith("--token="):
            return arg.split("=", 1)[1].strip()

    if os.environ.get("MEETRUDI_TTS_TOKEN"):
        return _unwrap(os.environ["MEETRUDI_TTS_TOKEN"])

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, encoding="utf-8") as f:
            token = _unwrap(f.read())
        if token:
            return token

    out = _capture(["aws", "secretsmanager", "get-secret-value", "--secret-id", SECRET_ID,
                    "--region", REGION, "--profile", PROFILE,
                    "--query", "SecretString", "--output", "text"])
    token = _unwrap(out.stdout)
    if token:
        return token

    sys.exit(
        "!! no token available.\n"
        "   rudi-deployer cannot read %s (only the Lambda runner role can), so supply it once:\n\n"
        "       echo <the-token> > services/tts/.token\n\n"
        "   That file is git-ignored. Alternatives: set MEETRUDI_TTS_TOKEN, or pass\n"
        "       python services/tts/generate_samples.py --token=<the-token>" % SECRET_ID)


def _synth(url, token, text, voice, speaker_id=None, length_scale=None, gap_ms=SENTENCE_GAP_MS):
    payload = {"token": token, "text": text, "voice": voice, "sentence_gap_ms": gap_ms}
    if speaker_id is not None:
        payload["speaker_id"] = speaker_id
    if length_scale is not None:
        payload["length_scale"] = length_scale
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body, int((time.time() - started) * 1000)


INDEX_HEAD = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rudi voice samples</title>
<style>
:root{--ink:#263d7d;--muted:#6f7688;--bg:#f0f0f0;--card:#fff;--bd:#d9d9d9;--pink:#e925c9}
@media(prefers-color-scheme:dark){:root{--ink:#c3cfef;--muted:#98a0b6;--bg:#121621;--card:#1a1f2e;--bd:#2e3547;--pink:#f558d6}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);margin:0;padding:2rem 1.25rem 5rem;
 font:16px/1.6 "Nunito",ui-rounded,"Segoe UI",system-ui,sans-serif}
.w{max-width:52rem;margin:0 auto}
h1{font-size:1.7rem;font-weight:800;text-transform:uppercase;letter-spacing:-.01em;margin:0 0 .3rem}
h2{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:2.2rem 0 .8rem}
p.sub{color:var(--muted);margin:0 0 1.5rem}
.card{background:var(--card);border:1px solid var(--bd);border-radius:.75rem;padding:.9rem 1.1rem;margin-bottom:.7rem}
.card.flem{border-left:3px solid var(--pink)}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap}
.name{font-family:ui-monospace,Consolas,monospace;font-size:.88rem;font-weight:700}
.meta{font-size:.78rem;color:var(--muted)}
audio{width:100%;margin-top:.6rem}
blockquote{border-left:3px solid var(--bd);margin:0 0 1.5rem;padding:.2rem 0 .2rem 1rem;
 color:var(--muted);font-size:.9rem}
</style></head><body><div class="w">
<h1>Rudi voice samples</h1>
<p class="sub">Piper on CPU Lambda. Flemish voices are marked with the pink rail &mdash; that is the pilot cohort's accent.</p>
"""


def _index(results):
    parts = [INDEX_HEAD]
    for lang, title, text in (("nl", "Dutch — Flemish first", TEXT_NL),
                              ("en", "English", TEXT_EN)):
        parts.append('<h2>%s</h2>' % title)
        parts.append('<blockquote>%s</blockquote>' % text[:180].replace("&", "&amp;") + "&hellip;</blockquote>")
        for r in results:
            if r["lang"] != lang or not r["ok"]:
                continue
            flem = " flem" if r["voice"].startswith("nl_BE") else ""
            parts.append(
                '<div class="card%s"><div class="row"><span class="name">%s</span>'
                '<span class="meta">%s &middot; %s &middot; %.1fs audio &middot; synth %sms</span></div>'
                '<audio controls preload="none" src="%s"></audio></div>'
                % (flem, r["voice"], r["label"], r["lang"], r["seconds"], r["synth_ms"], r["file"]))
    parts.append("</div></body></html>")
    return "\n".join(parts)


def main():
    url = _stack_output("FunctionUrl")
    if not url:
        sys.exit("!! could not resolve the %s Function URL — is the stack deployed?" % STACK)
    token = _token()
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Rendering %d samples -> %s   (sentence gap %dms)\n"
          % (len(SAMPLES), OUT_DIR, SENTENCE_GAP_MS))
    results = []
    for spec in SAMPLES:
        voice, lang, label = spec["voice"], spec["lang"], spec["label"]
        text = spec.get("text") or (TEXT_NL if lang == "nl" else TEXT_EN)
        suffix = spec.get("suffix")
        base = {"voice": voice, "lang": lang, "label": label, "suffix": suffix}

        sys.stdout.write("  %-28s %-10s " % (voice, suffix or ""))
        sys.stdout.flush()
        try:
            body, wall_ms = _synth(url, token, text, voice,
                                   spec.get("speaker_id"), spec.get("length_scale"))
        except Exception as e:  # noqa: BLE001
            print("FAILED — %s" % e)
            results.append(dict(base, ok=False))
            continue
        if not body.get("ok"):
            print("FAILED — %s" % body.get("error"))
            results.append(dict(base, ok=False))
            continue

        # Long renders exceed the 6MB Function URL cap and come back as a presigned S3 URL.
        if body.get("audio_b64"):
            audio = base64.b64decode(body["audio_b64"])
        else:
            with urllib.request.urlopen(body["audio_url"], timeout=120) as r:
                audio = r.read()
        fname = "%s-%s%s.wav" % (lang, voice, "-" + suffix if suffix else "")
        with open(os.path.join(OUT_DIR, fname), "wb") as f:
            f.write(audio)

        # 44-byte RIFF header, 16-bit mono
        seconds = max(0.0, (len(audio) - 44) / 2.0 / float(body.get("sample_rate") or 22050))
        t = body.get("timings", {})
        print("ok  %5.1fs audio  synth %5sms  load %5sms  wall %5sms  %6.1f KB"
              % (seconds, t.get("synth_ms"), t.get("load_ms"), wall_ms, len(audio) / 1024))
        results.append(dict(base, ok=True, file=fname, seconds=seconds,
                            synth_ms=t.get("synth_ms"), load_ms=t.get("load_ms"),
                            wall_ms=wall_ms))

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(_index(results))

    ok = [r for r in results if r["ok"]]
    print("\n%d/%d rendered. Open %s"
          % (len(ok), len(results), os.path.join(OUT_DIR, "index.html")))
    if ok:
        rtf = sum(r["synth_ms"] for r in ok) / 1000.0 / sum(r["seconds"] for r in ok)
        print("Mean real-time factor: %.2fx  (lower is faster; 1.0 = real time)" % rtf)


if __name__ == "__main__":
    main()
