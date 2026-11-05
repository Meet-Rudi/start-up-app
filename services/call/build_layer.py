#!/usr/bin/env python3
"""
Build the meetrudi-tenant-prod-pool-engine Lambda layer: the platform Turn Engine and CM's clone
(packages/rudi_platform, rudi_skills, rudi_tenants), for meetrudi-call-ws (decision #37).

Pure Python and standard library only, so this is a copy rather than a pip install: no wheels,
no platform flags, nothing that differs between this Windows machine and Lambda. Tests and
caches are left out.

Run by deploy.py before `sam build`; safe to run by hand:

    python services/call/build_layer.py

Result: services/call/layer/python/ (git-ignored), shipped by template.yaml as-is.
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LAYER_DIR = os.path.join(HERE, "layer")
TARGET = os.path.join(LAYER_DIR, "python")
PACKAGES = ("rudi_platform", "rudi_skills", "rudi_tenants")
SKIP = shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache",
                              ".ruff_cache")


def main():
    if os.path.isdir(LAYER_DIR):
        shutil.rmtree(LAYER_DIR)
    os.makedirs(TARGET, exist_ok=True)
    for name in PACKAGES:
        source = os.path.join(ROOT, "packages", name)
        if not os.path.isfile(os.path.join(source, "__init__.py")):
            sys.exit(f"!! {source} is not a package")
        shutil.copytree(source, os.path.join(TARGET, name), ignore=SKIP)

    # Fail here, not on a live call: the layer must import and load CM's clone on its own.
    sys.dont_write_bytecode = True          # the check must not leave caches in the layer
    sys.path.insert(0, TARGET)
    try:
        import rudi_skills
        from rudi_platform.registry import InMemoryRegistryStore
        from rudi_tenants import cm

        seeded = cm.seed(InMemoryRegistryStore(), rudi_skills.manifests())
        rudi_skills.runtime()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"!! the layer does not load: {type(e).__name__}: {e}")
    finally:
        sys.path.remove(TARGET)

    files = sum(len(f) for _r, _d, f in os.walk(TARGET))
    print(f"Layer built: {TARGET} ({files} files, CM clone version {seeded.version_id} loads)")


if __name__ == "__main__":
    main()
