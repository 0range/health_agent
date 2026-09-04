from pathlib import Path

import pytest

from health_agent.vault import FileVault, VaultIntegrityError


def test_same_bytes_have_one_vault_object(tmp_path: Path) -> None:
    source_a = tmp_path / "a.pdf"
    source_b = tmp_path / "b.pdf"
    source_a.write_bytes(b"same")
    source_b.write_bytes(b"same")

    vault = FileVault(tmp_path / "vault")
    first = vault.store(source_a)
    second = vault.store(source_b)

    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert first.path.read_bytes() == b"same"
    assert first.size_bytes == 4
    assert first.path.stat().st_mode & 0o777 == 0o600
    assert vault.root.stat().st_mode & 0o777 == 0o700
    assert first.path.parent.stat().st_mode & 0o777 == 0o700


def test_corrupt_existing_object_is_rejected_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "analysis.pdf"
    source.write_bytes(b"trusted original")
    vault = FileVault(tmp_path / "vault")
    stored = vault.store(source)
    stored.path.write_bytes(b"corrupt")

    with pytest.raises(VaultIntegrityError):
        vault.store(source)

    assert source.read_bytes() == b"trusted original"
    assert stored.path.read_bytes() == b"corrupt"
