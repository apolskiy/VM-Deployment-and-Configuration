"""Print the resolved inventory key and manifest paths, one per line.

Helper for scripts/manifest.ps1: it avoids embedding a multi-line or quoted
``python -c`` argument in PowerShell, which mangles quotes when handed to a
native executable. Run from the repository root.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

# pylint: disable=wrong-import-position
from vmdeploy.config import load_cluster_config  # noqa: E402

_CONFIG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config/cluster.toml")


def main() -> None:
    """Load the configuration and print the key and manifest paths."""
    config = load_cluster_config(_CONFIG)
    print(config.inventory.key_file)
    print(config.inventory.manifest_file)


if __name__ == "__main__":
    main()
