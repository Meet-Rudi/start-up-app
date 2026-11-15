#!/usr/bin/env python3
"""
Build the meetrudi-internal-prod-db-migrate-deps Lambda layer (decision #42):

    python/        PyMySQL (a pure-Python MySQL driver) and certifi, for Lambda's platform
                   (linux x86_64, CPython 3.12), plus packages/rudi_platform (the runner itself)
    migrations/    a copy of db/migrations, read at /opt/migrations

PyMySQL is pure Python and needs nothing else for our connection (verified TLS from an
ssl.SSLContext, no RSA key exchange), so the zip built on this Windows machine runs on Lambda;
pip's --platform flags make sure no Windows wheel slips in. The build fails here, not on the
first invoke, if the layer cannot import the runner or the runner refuses the migrations.

Run by deploy.py before `sam build`; safe to run by hand:

    python services/db-migrate/build_layer.py

Result: services/db-migrate/layer/ (git-ignored), shipped by template.yaml as-is.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LAYER_DIR = os.path.join(HERE, "layer")
TARGET = os.path.join(LAYER_DIR, "python")
MIGRATIONS = os.path.join(LAYER_DIR, "migrations")
# Pinned, established release; certifi floats on purpose so the CA bundle stays current.
REQUIREMENTS = ["PyMySQL==1.1.1", "certifi"]
SKIP = shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache",
                              ".ruff_cache")


def main():
    if os.path.isdir(LAYER_DIR):
        shutil.rmtree(LAYER_DIR)
    os.makedirs(TARGET, exist_ok=True)

    cmd = [sys.executable, "-m", "pip", "install", "--target", TARGET, "--python-version", "3.12",
           "--only-binary=:all:", "--no-compile", "--no-deps", "--platform", "manylinux_2_17_x86_64",
           "--platform", "manylinux2014_x86_64", *REQUIREMENTS]
    print(">", " ".join(cmd))
    if subprocess.run(cmd, check=False).returncode != 0:
        sys.exit("!! layer build failed (pip)")

    shutil.copytree(os.path.join(ROOT, "packages", "rudi_platform"),
                    os.path.join(TARGET, "rudi_platform"), ignore=SKIP)
    shutil.copytree(os.path.join(ROOT, "db", "migrations"), MIGRATIONS)

    for root, dirs, _files in os.walk(TARGET):
        for name in list(dirs):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)

    # The layer must import on its own and accept the migrations it carries.
    sys.dont_write_bytecode = True
    sys.path.insert(0, TARGET)
    try:
        import pymysql  # noqa: F401
        from rudi_platform.db import load

        loaded = load(MIGRATIONS)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"!! the layer does not load: {type(e).__name__}: {e}")
    finally:
        sys.path.remove(TARGET)
    print(f"Layer built: {LAYER_DIR} ({len(loaded)} migrations: "
          f"{loaded[0].name} to {loaded[-1].name})")


if __name__ == "__main__":
    main()
