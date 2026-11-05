#!/usr/bin/env python3
"""
Build the meetrudi-tenant-prod-pool-engine Lambda layer for meetrudi-call-ws (decision #37):
packages/build_engine_layer.py for this service. Run by deploy.py before `sam build`; safe to
run by hand:

    python services/call/build_layer.py

Result: services/call/layer/python/ (git-ignored), shipped by template.yaml as-is.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "packages"))

from build_engine_layer import build

if __name__ == "__main__":
    build(HERE)
