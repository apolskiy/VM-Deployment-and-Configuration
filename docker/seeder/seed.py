"""Seed the container stack's encrypted inventory before the service starts.

Generates the AES key (if absent) and writes an encrypted datastore holding the
three cluster hosts, using the same :mod:`vmdeploy.inventory` code and wire
format as the VM path. Runs once as a one-shot compose service; the inventory
service then reads what it produced. This is what lets the same E2E suite
validate the container stack unchanged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

# pylint: disable=wrong-import-position
from vmdeploy.inventory import (  # noqa: E402
    InventoryRecord,
    encrypt_records,
    load_or_create_key,
    utc_timestamp,
)

_HOSTS = (
    ("apjump", "jump", "172.28.0.10"),
    ("apnode1", "backend", "172.28.0.11"),
    ("apnode2", "backend", "172.28.0.12"),
)


def main() -> None:
    """Generate the key and write the encrypted, seeded datastore."""
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    key = load_or_create_key(data_dir / "inventory.key")
    timestamp = utc_timestamp()
    records = tuple(
        InventoryRecord(
            hostname=hostname,
            role=role,
            ipv4=ipv4,
            ipv6="",
            status="Deployed",
            status_timestamp=timestamp,
            state="Active",
            state_timestamp=timestamp,
        )
        for hostname, role, ipv4 in _HOSTS
    )

    store = data_dir / "inventory.enc"
    store.write_bytes(encrypt_records(records, key))
    # The service may run under a different uid; keep the demo files group/other
    # readable and writable so it can decrypt and upsert.
    for path in (data_dir / "inventory.key", store):
        try:
            path.chmod(0o666)
        except OSError:
            pass
    print(f"Seeded {len(records)} inventory record(s) to {store}")


if __name__ == "__main__":
    main()
