#!/usr/bin/env python3
"""
Repo-local entry point for AIPQ's own CI (see .github/workflows/ci.yml's
aipq-self-eval job) — delegates to the real implementation in sdk/aipq/cli.py
so this repo can `python3 cli/aipq_cli.py ...` without a pip install, while
that same code also ships as the `aipq` console script once `aipq-sdk` is
installed from PyPI. Keep logic in sdk/aipq/cli.py, not here.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk"))

from aipq.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
