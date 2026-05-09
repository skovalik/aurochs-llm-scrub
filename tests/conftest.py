"""Shared pytest configuration.

Adds the project root to ``sys.path`` so tests can ``import
aurochs_llm_scrub`` without a package install. When the spine ships
with an editable install in CI this can go away.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
