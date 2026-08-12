#!/usr/bin/env python3
"""Public convenience entrypoint for the repository-memory installer."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "skills" / "repository-memory" / "scripts" / "install.py"
    # runpy preserves the caller's import path; the installer imports its
    # sibling ``version.py`` and other stdlib-only modules from this directory.
    # Make the public root entrypoint behave exactly like invoking the script.
    sys.path.insert(0, str(target.parent))
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
