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


@pytest.fixture
def synthetic_jpeg(tmp_path: Path) -> Path:
    path = tmp_path / "photo.jpg"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=100)
        page.insert_text((20, 30), "Ferritin 42 ng/mL")
        path.write_bytes(page.get_pixmap().tobytes("jpeg"))
    return path


def test_single_complete_jpeg_accepts_marker_bytes_inside_metadata(
    synthetic_jpeg, monkeypatch
):
    original = synthetic_jpeg.read_bytes()
    payload = b"metadata with embedded markers \xff\xd8\xff\xd9"
    comment = b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload
    synthetic_jpeg.write_bytes(original[:2] + comment + original[2:])
    monkeypatch.setattr("health_agent.images.recognize_image", lambda _: None)
    assert extract_image(synthetic_jpeg)[0] == "image/jpeg"


@pytest.mark.parametrize("suffix", ["second_jpeg", "trailing_payload", "extra_eoi"])
def test_jpeg_rejects_trailing_stream_before_decode_or_ocr(
    synthetic_jpeg, monkeypatch, suffix
):
    original = synthetic_jpeg.read_bytes()
    trailing = {
        "second_jpeg": original,
        "trailing_payload": b"private appended payload\xff\xd9",
        "extra_eoi": b"\xff\xd9",
    }[suffix]
    synthetic_jpeg.write_bytes(original + trailing)
    calls = []
    monkeypatch.setattr(
        "health_agent.images.pymupdf.Pixmap", lambda _: calls.append("decode")
    )
    monkeypatch.setattr(
        "health_agent.images.recognize_image", lambda _: calls.append("ocr")
    )
    with pytest.raises(ValueError):
        extract_image(synthetic_jpeg)
    assert calls == []


@pytest.mark.parametrize("variant", ["mpf_after_frame", "second_frame", "missing_eoi"])
def test_jpeg_checks_past_first_frame_header(synthetic_jpeg, variant, monkeypatch):
    original = synthetic_jpeg.read_bytes()
    scan_start = original.index(b"\xff\xda")
    frame_start = next(
        original.index(marker)
        for marker in (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2")
        if marker in original
    )
    frame_length = (
        int.from_bytes(original[frame_start + 2 : frame_start + 4], "big") + 2
    )
    bad_data = {
        "mpf_after_frame": original[:scan_start]
        + b"\xff\xe2\x00\x06MPF\x00"
        + original[scan_start:],
        "second_frame": original[:scan_start]
        + original[frame_start : frame_start + frame_length]
        + original[scan_start:],
        "missing_eoi": original[:-2],
    }[variant]
    synthetic_jpeg.write_bytes(bad_data)
    monkeypatch.setattr("health_agent.images.recognize_image", lambda _: None)
    with pytest.raises(ValueError):
        extract_image(synthetic_jpeg)
