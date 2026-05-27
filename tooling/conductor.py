#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

from orchestrator.cli import main


if "TV7_STATE_ROOT_NAME" not in os.environ:
    os.environ["TV7_STATE_ROOT_NAME"] = ".conductor"

raise SystemExit(main(sys.argv[1:]))
