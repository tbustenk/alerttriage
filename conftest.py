"""Pytest bootstrap: put the parent of the repo on sys.path.

The package is imported as ``alerttriage.*`` even though the on-disk
layout is flat (the repo directory itself plays the role of the
``alerttriage`` package). For those imports to resolve when pytest is
invoked from inside the repo, the *parent* of the repo must be on
``sys.path`` — which is what this file does.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_PARENT = _REPO_ROOT.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))
