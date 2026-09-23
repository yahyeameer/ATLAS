"""Hermes entry point of the ATLAS plugin. The code lives in the atlas package (atlas_plugins.hooks)."""

import sys
from pathlib import Path

try:
    import atlas_plugins  # noqa: F401
except ImportError:  # Hermes' own interpreter: use the ATLAS checkout the installer recorded
    sys.path.append((Path(__file__).resolve().parent / "ATLAS_ROOT").read_text().strip())

from atlas_plugins.hooks import register  # noqa: E402,F401
