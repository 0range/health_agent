import hashlib
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest

from health_agent.lab_extraction.local import read_page
from health_agent.lab_extraction.types import DocumentSnapshot, ExtractionError
from health_agent.vault import FileVault


def original(tmp_path: Path, *, image=False):
    path = tmp_path / ("synthetic.png" if image else "synthetic.pdf")
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=100)
        page.insert_text((20, 30), "Glucose 5.1 mmol/L")
        if image:
            page.get_pixmap().save(path)
        else:
            document.save(path)
    vault = FileVault(tmp_path / "vault")
    stored = vault.store(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return DocumentSnapshot(
        uuid4(),
        uuid4(),
        digest,
        str(stored.path),
        "image/png" if image else "application/pdf",
    ), vault.root


def test_local_digital_text_requires_no_ocr(tmp_path, monkeypatch):
    snapshot, root = original(tmp_path)
    monkeypatch.setattr(
        "health_agent.lab_extraction.local.recognize",
        lambda _: pytest.fail("digital page must not OCR"),
    )
    assert "Glucose 5.1 mmol/L" in read_page(snapshot, 1, root, tmp_path / "temporary")


def test_image_ocr_private_temp_cleanup(tmp_path, monkeypatch):
    snapshot, root = original(tmp_path, image=True)
    seen = []

    def recognize(path):
        assert path.stat().st_mode & 0o777 == 0o600
        seen.append(path)
        return "Глюкоза 5,1 ммоль/л"

    monkeypatch.setattr("health_agent.lab_extraction.local.recognize", recognize)
    assert read_page(snapshot, 1, root, tmp_path / "temporary") == "Глюкоза 5,1 ммоль/л"
    assert seen and not seen[0].exists()


@pytest.mark.parametrize("failure", ["hash", "outside", "symlink", "ocr"])
def test_local_failures_are_safe_and_leave_no_temporary_files(
    tmp_path, monkeypatch, failure
):
    from dataclasses import replace

    snapshot, root = original(tmp_path, image=True)
    path = Path(snapshot.vault_path)
    if failure == "hash":
        path.write_bytes(b"corrupted synthetic data")
    elif failure == "outside":
        snapshot = replace(snapshot, vault_path=str(tmp_path / "outside.png"))
    elif failure == "symlink":
        outside = tmp_path / "outside.png"
        path.rename(outside)
        path.symlink_to(outside)
    monkeypatch.setattr("health_agent.lab_extraction.local.recognize", lambda _: None)
    with pytest.raises(ExtractionError) as error:
        read_page(snapshot, 1, root, tmp_path / "temporary")
    assert str(tmp_path) not in str(error.value)
    assert not list((tmp_path / "temporary").glob("**/*.png"))


def test_raster_pixel_bomb_is_rejected_before_native_open(tmp_path, monkeypatch):
    from dataclasses import replace

    snapshot, root = original(tmp_path, image=True)
    content = bytearray(Path(snapshot.vault_path).read_bytes())
    content[16:20] = (100_000).to_bytes(4, "big")
    content[20:24] = (100_000).to_bytes(4, "big")
    source = tmp_path / "oversized.png"
    source.write_bytes(content)
    stored = FileVault(root).store(source)
    snapshot = replace(snapshot, sha256=stored.sha256, vault_path=str(stored.path))
    monkeypatch.setattr(
        "health_agent.lab_extraction.local.pymupdf.open",
        lambda **_: pytest.fail("must check raster dimensions before native decode"),
    )
    with pytest.raises(ExtractionError, match="page_pixel_limit"):
        read_page(snapshot, 1, root, tmp_path / "temporary")


def test_local_ocr_subprocess_is_bounded_and_never_uses_shell(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from health_agent.lab_extraction.local import recognize

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="synthetic text", stderr="")

    monkeypatch.setattr("health_agent.lab_extraction.local.sys.platform", "darwin")
    monkeypatch.setattr("health_agent.lab_extraction.local.subprocess.run", run)
    assert recognize(tmp_path / "input.png") == "synthetic text"
    command, options = calls[0]
    assert command[0] == "/usr/bin/swift"
    assert options["timeout"] == 30
    assert not options.get("shell", False)
