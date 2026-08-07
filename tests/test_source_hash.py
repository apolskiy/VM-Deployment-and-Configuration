"""Unit tests for the Go source fingerprint that gates recompilation.

The golden image bakes a binary tagged with this hash; deploy recompiles only
when the local sources no longer match. These tests pin the properties that
make that gate trustworthy: it is stable across calls, sensitive to any source
change, and complete over every file that affects the binary.
"""

from __future__ import annotations

from pathlib import Path

import allure
import pytest

from vmdeploy.exceptions import VmDeployError
from vmdeploy.inventory_service import _HASH_FILES, source_hash


def _seed_sources(root: Path) -> None:
    """Create a minimal set of files matching the hashed source layout.

    Args:
        root: Directory to populate.
    """
    (root / "templates").mkdir(parents=True, exist_ok=True)
    for name in _HASH_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {name}\n", encoding="utf-8")


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Source fingerprint")
class TestSourceHash:
    """The fingerprint is deterministic, sensitive, and complete."""

    @allure.title("The same sources always hash to the same value")
    def test_stable(self, tmp_path: Path) -> None:
        """Hashing is deterministic across calls.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        _seed_sources(tmp_path)
        assert source_hash(tmp_path) == source_hash(tmp_path)

    @allure.title("Changing any source file changes the hash")
    @pytest.mark.parametrize("changed", list(_HASH_FILES))
    def test_sensitive_to_each_file(self, tmp_path: Path, changed: str) -> None:
        """A change to any hashed file changes the fingerprint.

        Args:
            tmp_path: Pytest-provided temporary directory.
            changed: The file mutated for this case.
        """
        _seed_sources(tmp_path)
        before = source_hash(tmp_path)
        (tmp_path / changed).write_text("mutated\n", encoding="utf-8")
        assert source_hash(tmp_path) != before

    @allure.title("A missing source file is an error")
    def test_missing_source(self, tmp_path: Path) -> None:
        """An incomplete source tree cannot produce a fingerprint.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        _seed_sources(tmp_path)
        (tmp_path / "main.go").unlink()
        with pytest.raises(VmDeployError, match="source missing"):
            source_hash(tmp_path)

    @allure.title("The real repository sources hash without error")
    def test_real_sources(self) -> None:
        """The shipped Go sources produce a stable 64-char hex digest."""
        digest = source_hash(Path("goservice"))
        assert len(digest) == 64
        assert digest == source_hash(Path("goservice"))
