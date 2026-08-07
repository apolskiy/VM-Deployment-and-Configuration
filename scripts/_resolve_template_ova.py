"""Print the resolved golden OVA path and its published image reference.

Helper for scripts/publish-image.ps1 and scripts/pull-image.ps1, so neither
hardcodes a location or a registry that can drift from the configuration.
Follows the same pattern as _resolve_inventory_paths.py: it avoids embedding a
quoted ``python -c`` argument in PowerShell, which mangles quotes when handed to
a native executable. Run from the repository root.

Prints two lines: the OVA path, then the OCI reference.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "src")

# pylint: disable=wrong-import-position
from vmdeploy.config import load_cluster_config  # noqa: E402

_CONFIG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config/cluster.toml")


def main() -> None:
    """Load the configuration and print the OVA path and image reference."""
    config = load_cluster_config(_CONFIG)
    print(config.virtualbox.template_ova)
    print(config.virtualbox.template_image_ref)


if __name__ == "__main__":
    main()
