#!/usr/bin/env python3
"""
Build the meetrudi-internal-prod-prospect-intake-deps Lambda layer:

    python/rudi_platform    the platform core (ids, S3 key builders); standard library only

Run by deploy.py before `sam build`; safe to run by hand:

    python services/prospect-intake/build_layer.py

Result: services/prospect-intake/layer/ (git-ignored), shipped by template.yaml as-is. The build
fails here, not on the first request, if the handler cannot import with this layer.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LAYER_DIR = os.path.join(HERE, "layer")
TARGET = os.path.join(LAYER_DIR, "python")
SKIP = shutil.ignore_patterns(
    "tests", "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache"
)

# Imports the handler as Lambda would (layer + src, stub AWS clients; Lambda provides boto3 and
# botocore) and checks the questionnaire, in a fresh interpreter so nothing from this one leaks in.
_PROBE = """
import os, sys, types
sys.path[:0] = [{target!r}, {src!r}]
stub = types.ModuleType("boto3"); stub.client = lambda *a, **k: None
sys.modules["boto3"] = stub
config = types.ModuleType("botocore.config"); config.Config = lambda **k: None
sys.modules["botocore"] = types.ModuleType("botocore"); sys.modules["botocore.config"] = config
os.environ["DATA_BUCKET"] = "probe"
import handler
print(handler.Q["questionnaire"], handler.Q["form_version"], len(handler.Q["steps"]), "steps")
"""


def main():
    if os.path.isdir(LAYER_DIR):
        shutil.rmtree(LAYER_DIR)
    os.makedirs(TARGET, exist_ok=True)
    shutil.copytree(
        os.path.join(ROOT, "packages", "rudi_platform"),
        os.path.join(TARGET, "rudi_platform"),
        ignore=SKIP,
    )

    probe = _PROBE.format(target=TARGET, src=os.path.join(HERE, "src"))
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.exit(
            "!! the handler does not load with this layer:\n" + result.stderr.strip()
        )
    print(f"Layer built: {LAYER_DIR} (questionnaire {result.stdout.strip()})")


if __name__ == "__main__":
    main()
