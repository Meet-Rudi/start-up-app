#!/usr/bin/env python3
"""
Build the meetrudi-piper Lambda layer.

Piper needs native wheels (onnxruntime, the espeak bridge), so the layer must be assembled
for Lambda's platform, not this machine's. pip's --platform flags do that without Docker,
which matters because the deploy workstation has none.

Run by deploy.py before `sam build`; safe to run by hand:

    python services/tts/build_layer.py

Result: services/tts/layer/python/ , which template.yaml ships verbatim as a LayerVersion.

Size budget — Lambda allows 250 MB unzipped across the function and all its layers:

    onnxruntime   53 MB
    piper         45 MB   (24 MB after dropping the Hebrew models)
    numpy         58 MB
    everything else ~2 MB
    ------------------------------
    ~136 MB, leaving ~110 MB of headroom.

Voice models are NOT in here. Five voices are 313 MB on their own; they live in S3 and are
pulled into /tmp on first use (see src/app.py).

If a future wheel ever refuses to resolve for manylinux, the fallback is to run this same
script in AWS CloudShell — it is Amazon Linux, so plain `pip install --target` produces
Lambda-compatible binaries — then zip `layer/` and upload it.
"""

import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER_DIR = os.path.join(HERE, "layer")
TARGET = os.path.join(LAYER_DIR, "python")

PIPER_VERSION = "1.6.0"
PY_VERSION = "3.12"
PLATFORMS = ["manylinux_2_28_x86_64", "manylinux2014_x86_64"]

# Dropped after install. `hebrew/` is imported lazily (only for PhonemeType.HEBREW), so it is
# safe to remove for a European deployment. `tashkeel/` is NOT — piper/voice.py imports it at
# module level and the layer breaks without it.
PRUNE = [
    os.path.join("piper", "hebrew"),
    os.path.join("piper", "img"),
    os.path.join("piper", "train"),
]


def _run(cmd):
    print(">", " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        sys.exit("!! layer build failed")


def _size_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024 * 1024)


def main():
    if os.path.isdir(LAYER_DIR):
        shutil.rmtree(LAYER_DIR)
    os.makedirs(TARGET, exist_ok=True)

    cmd = [sys.executable, "-m", "pip", "install",
           "--target", TARGET,
           "--python-version", PY_VERSION,
           "--only-binary=:all:",
           "--no-compile",
           "piper-tts==" + PIPER_VERSION]
    for plat in PLATFORMS:
        cmd += ["--platform", plat]
    _run(cmd)

    before = _size_mb(TARGET)
    for rel in PRUNE:
        path = os.path.join(TARGET, rel)
        if os.path.isdir(path):
            shutil.rmtree(path)
            print("   pruned", rel)

    # Compiled caches and test suites add weight and nothing else.
    for root, dirs, _files in os.walk(TARGET):
        for name in list(dirs):
            if name in ("__pycache__", "tests"):
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)

    after = _size_mb(TARGET)
    print("\nLayer built: %s" % TARGET)
    print("  before prune : %.1f MB" % before)
    print("  after prune  : %.1f MB   (Lambda ceiling is 250 MB incl. function)" % after)
    if after > 230:
        sys.exit("!! layer is too close to the 250 MB limit — prune further before deploying")


if __name__ == "__main__":
    main()
