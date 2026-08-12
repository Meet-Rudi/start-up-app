#!/usr/bin/env python3
"""
Upload Piper voice models to the shared data bucket. Run once (and again when adding a voice):

    python services/tts/seed_voices.py

The five voices total ~313 MB, which is why they are not baked into the Lambda layer. They land
at s3://<data-bucket>/voices/piper/ and are pulled into /tmp on first use by meetrudi-tts.

Voices are MIT-licensed, from rhasspy/piper-voices on Hugging Face. `nl_BE-nathalie` is the one
that matters most for the pilot: it is Belgian Flemish, not Netherlands Dutch.
"""

import os
import sys
import subprocess
import urllib.request

REGION = "eu-central-1"
PROFILE = "rudi-deployer"
PREFIX = "voices/piper"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".voice-cache")

URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
       "{family}/{code}/{name}/{quality}/{code}-{name}-{quality}{ext}")

VOICES = [
    # --- production defaults -------------------------------------------------
    ("en", "en_US", "lessac", "medium"),
    ("nl", "nl_BE", "nathalie", "medium"),   # Belgian Flemish — the pilot voice
    ("nl", "nl_NL", "mls", "medium"),
    ("fr", "fr_FR", "siwis", "medium"),
    ("de", "de_DE", "thorsten", "medium"),
    # --- comparison set, for choosing Rudi's voice ---------------------------
    # Dutch: this is the entire realistic space. Piper has exactly two Flemish voices.
    ("nl", "nl_BE", "rdh", "medium"),
    ("nl", "nl_NL", "alex", "medium"),
    ("nl", "nl_NL", "pim", "medium"),
    ("nl", "nl_NL", "ronnie", "medium"),
    # English: 5 of 31, picked for a warm coaching register rather than breadth.
    ("en", "en_US", "amy", "medium"),
    ("en", "en_US", "ryan", "high"),
    ("en", "en_US", "ryan", "medium"),   # same speaker, cleaner and ~5x faster than high
    ("en", "en_GB", "alan", "medium"),
    ("en", "en_GB", "jenny_dioco", "medium"),
]


def _bucket():
    out = subprocess.run(
        ["aws", "cloudformation", "describe-stacks", "--stack-name", "meetrudi-base",
         "--region", REGION, "--profile", PROFILE,
         "--query", "Stacks[0].Outputs[?OutputKey=='DataBucketName'].OutputValue",
         "--output", "text"],
        capture_output=True, text=True)
    name = (out.stdout or "").strip()
    if not name:
        sys.exit("!! could not resolve the data bucket — is meetrudi-base deployed?")
    return name


def _download(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print("   cached  %s" % os.path.basename(path))
        return
    print("   fetch   %s" % os.path.basename(path))
    req = urllib.request.Request(url, headers={"User-Agent": "meetrudi-seed-voices"})
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            f.write(chunk)


def main():
    bucket = _bucket()
    os.makedirs(CACHE, exist_ok=True)
    print("Seeding %d voices -> s3://%s/%s/\n" % (len(VOICES), bucket, PREFIX))

    for family, code, name, quality in VOICES:
        voice = "%s-%s-%s" % (code, name, quality)
        print("%s" % voice)
        for ext in (".onnx", ".onnx.json"):
            url = URL.format(family=family, code=code, name=name, quality=quality, ext=ext)
            local = os.path.join(CACHE, voice + ext)
            _download(url, local)
            key = "%s/%s%s" % (PREFIX, voice, ext)
            r = subprocess.run(
                ["aws", "s3", "cp", local, "s3://%s/%s" % (bucket, key),
                 "--region", REGION, "--profile", PROFILE, "--only-show-errors"])
            if r.returncode != 0:
                sys.exit("!! upload failed for %s" % key)
            print("   upload  s3://%s/%s" % (bucket, key))
        print()

    print("Done. meetrudi-tts resolves short keys: en, nl (= nl_BE Flemish), nl_NL, fr, de.")
    print("Local cache kept at %s — delete it to force a re-download." % CACHE)


if __name__ == "__main__":
    main()
