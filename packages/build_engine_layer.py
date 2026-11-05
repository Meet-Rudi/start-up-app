#!/usr/bin/env python3
"""
Build a service's platform-engine Lambda layer: the platform Turn Engine and CM's clone
(packages/rudi_platform, rudi_skills, rudi_tenants). Used by services/call (layer
meetrudi-tenant-prod-pool-engine, decision #37) and services/whatsapp
(meetrudi-tenant-prod-pool-engine-whatsapp, decision #40). Each service ships its own copy, so a
deploy carries exactly the engine its adapter was tested with.

Pure Python and standard library only, so this is a copy rather than a pip install: no wheels,
no platform flags, nothing that differs between this Windows machine and Lambda. Tests and
caches are left out.

    python packages/build_engine_layer.py services/call
    python packages/build_engine_layer.py services/whatsapp

Result: <service>/layer/python/ (git-ignored), shipped by the service's template.yaml as-is.
deploy.py runs it through each service's build_layer.py before `sam build`.
"""

import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PACKAGES = ("rudi_platform", "rudi_skills", "rudi_tenants")
SKIP = shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache",
                              ".ruff_cache")


def build(service_dir):
    service_dir = os.path.abspath(os.path.join(ROOT, service_dir))
    layer_dir = os.path.join(service_dir, "layer")
    target = os.path.join(layer_dir, "python")
    if not os.path.isfile(os.path.join(service_dir, "template.yaml")):
        sys.exit(f"!! {service_dir} is not a service (no template.yaml)")
    if os.path.isdir(layer_dir):
        shutil.rmtree(layer_dir)
    os.makedirs(target, exist_ok=True)
    for name in PACKAGES:
        source = os.path.join(ROOT, "packages", name)
        if not os.path.isfile(os.path.join(source, "__init__.py")):
            sys.exit(f"!! {source} is not a package")
        shutil.copytree(source, os.path.join(target, name), ignore=SKIP)

    # Fail here, not on a live message: the layer must import and load CM's clone on its own.
    sys.dont_write_bytecode = True          # the check must not leave caches in the layer
    sys.path.insert(0, target)
    try:
        import rudi_skills
        from rudi_platform.registry import InMemoryRegistryStore
        from rudi_tenants import cm

        seeded = cm.seed(InMemoryRegistryStore(), rudi_skills.manifests())
        rudi_skills.runtime()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"!! the layer does not load: {type(e).__name__}: {e}")
    finally:
        sys.path.remove(target)

    files = sum(len(f) for _r, _d, f in os.walk(target))
    print(f"Layer built: {target} ({files} files, CM clone version {seeded.version_id} loads)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python packages/build_engine_layer.py <service dir>")
    build(sys.argv[1])
