#!/usr/bin/env python3
"""
Build the meetrudi-arena-deps Lambda layer: PyYAML for Lambda's platform (linux x86_64, CPython 3.12).

The arena reads YAML batch files. PyYAML ships platform wheels, so the layer is assembled with
pip's --platform flags rather than from this machine's own install — the same approach as
services/tts/build_layer.py, and no Docker needed.

Run by deploy.py before `sam build`; safe to run by hand:

    python services/training-arena/build_layer.py

Result: services/training-arena/layer/python/ (git-ignored), shipped by template.yaml as-is.
"""

import os
import sys
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER_DIR = os.path.join(HERE, "layer")
TARGET = os.path.join(LAYER_DIR, "python")
PYYAML = "PyYAML==6.0.2"


def main():
    if os.path.isdir(LAYER_DIR):
        shutil.rmtree(LAYER_DIR)
    os.makedirs(TARGET, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--target", TARGET, "--python-version", "3.12",
           "--only-binary=:all:", "--no-compile", "--platform", "manylinux_2_17_x86_64",
           "--platform", "manylinux2014_x86_64", PYYAML]
    print(">", " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        sys.exit("!! layer build failed")
    for root, dirs, _files in os.walk(TARGET):
        for name in list(dirs):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)
    print("Layer built: %s" % TARGET)


if __name__ == "__main__":
    main()
