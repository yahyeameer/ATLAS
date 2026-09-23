"""ATLAS dashboard routes, mounted by Hermes at /api/plugins/atlas/. Logic: atlas_plugins.dashboard."""

import sys
from pathlib import Path

try:
    import atlas_plugins  # noqa: F401
except ImportError:  # Hermes' own interpreter: use the ATLAS checkout the installer recorded
    sys.path.append((Path(__file__).resolve().parents[1] / "ATLAS_ROOT").read_text().strip())

from atlas_plugins.dashboard import router  # noqa: E402,F401
