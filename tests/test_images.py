from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from health_agent.images import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    extract_image,
    recognize_image,
)


@pytest.fixture
def synthetic_image(tmp_path: Path) -> Path:
    path = tmp_path / "photo.png"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=100)
        page.insert_text((20, 30), "Ferritin 42 ng/mL")
        page.get_pixmap().save(path)
    return path


def test_image_uses_signature_and_local_ocr(synthetic_image, monkeypatch):
    monkeypatch.setattr(
        "health_agent.images.recognize_image", lambda _: "Ferritin 42 ng/mL"
    )
    media_type, extracted = extract_image(synthetic_image)
    assert media_type == "image/png"
    assert extracted.extraction_method == "local_ocr"
    assert extracted.pages[0].page_number == 1
    assert extracted.pages[0].text == "Ferritin 42 ng/mL"


def test_no_local_ocr_is_truthful(synthetic_image, monkeypatch):
    monkeypatch.setattr("health_agent.images.recognize_image", lambda _: None)
    _, extracted = extract_image(synthetic_image)
    assert extracted.extraction_method == "ocr_required"
    assert extracted.pages[0].text == ""


def test_image_rejects_declared_mime_mismatch(synthetic_image):
    with pytest.raises(ValueError):
        extract_image(synthetic_image, "image/jpeg")


def test_invalid_and_oversized_images_fail_before_ocr(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "health_agent.images.recognize_image", lambda p: calls.append(p)
    )
    invalid = tmp_path / "bad.png"
    invalid.write_bytes(b"\x89PNG\r\n\x1a\ninvalid")
    with pytest.raises(ValueError):
        extract_image(invalid)
    oversized = tmp_path / "huge.png"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValueError):
        extract_image(oversized)
    assert calls == []


def test_pixel_limit_precedes_decode_and_ocr(synthetic_image, monkeypatch):
    monkeypatch.setattr(
        "health_agent.images.image_dimensions", lambda *_: (MAX_IMAGE_PIXELS + 1, 1)
    )
    with pytest.raises(ValueError):
        extract_image(synthetic_image)


def test_local_recognizer_has_no_shell_and_bounded_execution(
    synthetic_image, monkeypatch
):
    calls = []
    monkeypatch.setattr("health_agent.images.sys.platform", "darwin")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="recognized", stderr="")

    monkeypatch.setattr("health_agent.images.subprocess.run", run)
    assert recognize_image(synthetic_image) == "recognized"
    command, kwargs = calls[0]
    assert command[0] == "/usr/bin/swift"
    assert command[-1] == str(synthetic_image)
    assert kwargs["timeout"] == 30
    assert kwargs.get("shell", False) is False


def test_recognizer_failure_does_not_expose_details(synthetic_image, monkeypatch):
    monkeypatch.setattr("health_agent.images.sys.platform", "darwin")

    def run(*args, **kwargs):
        raise OSError("private local path and medical text")

    monkeypatch.setattr("health_agent.images.subprocess.run", run)
    assert recognize_image(synthetic_image) is None


@pytest.mark.parametrize("failure", ["timeout", "failed", "too_long"])
def test_recognizer_failures_are_bounded(synthetic_image, monkeypatch, failure):
    import subprocess

    def run(*_args, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private-path", 30)
        return SimpleNamespace(
            returncode=1 if failure == "failed" else 0,
            stdout="X" * 100_002,
            stderr="private diagnostic",
        )

    monkeypatch.setattr("health_agent.images.sys.platform", "darwin")
    monkeypatch.setattr("health_agent.images.subprocess.run", run)
    assert recognize_image(synthetic_image) is None


def test_animation_and_checksum_rejected(synthetic_image, monkeypatch):
    import zlib

    original = synthetic_image.read_bytes()
    kind_payload = b"acTL" + (2).to_bytes(4, "big") + bytes(4)
    chunk = (
        (8).to_bytes(4, "big")
        + kind_payload
        + zlib.crc32(kind_payload).to_bytes(4, "big")
    )
    synthetic_image.write_bytes(original[:33] + chunk + original[33:])
    with pytest.raises(ValueError):
        extract_image(synthetic_image)
    corrupt = bytearray(original)
    corrupt[-1] ^= 1
    synthetic_image.write_bytes(corrupt)
    with pytest.raises(ValueError):
        extract_image(synthetic_image)
