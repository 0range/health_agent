"""Bounded local rendering/OCR of an already imported, hash-verified original."""

from __future__ import annotations

import hashlib
import math
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf

from health_agent.lab_extraction.types import (
    MAX_PAGE_CHARACTERS,
    DocumentSnapshot,
    ExtractionError,
)

MAX_ORIGINAL_BYTES = 50 * 1024 * 1024
MAX_PIXELS = 25_000_000
_MIME = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpeg"}
_VISION = r"""
import Foundation
import Vision
import ImageIO
let url = URL(fileURLWithPath: CommandLine.arguments.last!)
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      CGImageSourceGetCount(source) == 1,
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { exit(1) }
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
let languages = (try? request.supportedRecognitionLanguages()) ?? []
request.recognitionLanguages = ["en-US", "ru-RU"].filter { languages.contains($0) }
do {
    try VNImageRequestHandler(cgImage: image).perform([request])
    let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        .joined(separator: "\n")
    print(String(text.prefix(60001)))
} catch { exit(1) }
"""


def _reject_symlinks(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ExtractionError("unsafe_extraction_path")


def _original(snapshot: DocumentSnapshot, vault_root: Path) -> bytes:
    if len(snapshot.sha256) != 64 or any(
        char not in "0123456789abcdef" for char in snapshot.sha256
    ):
        raise ExtractionError("vault_integrity")
    path = Path(snapshot.vault_path)
    expected = vault_root / snapshot.sha256[:2] / snapshot.sha256
    _reject_symlinks(path)
    _reject_symlinks(vault_root)
    if path.absolute() != expected.absolute():
        raise ExtractionError("vault_integrity")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ORIGINAL_BYTES:
            raise ExtractionError("original_size_limit")
        data = stream.read(MAX_ORIGINAL_BYTES + 1)
    if (
        len(data) > MAX_ORIGINAL_BYTES
        or hashlib.sha256(data).hexdigest() != snapshot.sha256
    ):
        raise ExtractionError("vault_integrity")
    return data


def recognize(path: Path) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/swift", "-e", _VISION, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if result.returncode:
        return None
    if len(result.stdout.strip()) > MAX_PAGE_CHARACTERS:
        raise ExtractionError("page_text_limit")
    return result.stdout.strip() or None


def _image_pixels(data: bytes, kind: str) -> int:
    if kind == "png":
        if data[8:16] != b"\x00\x00\x00\rIHDR" or len(data) < 33:
            raise ExtractionError("unreadable_original")
        return int.from_bytes(data[16:20], "big") * int.from_bytes(data[20:24], "big")
    offset = 2
    while offset + 4 <= len(data) and data[offset] == 0xFF:
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in (0xC0, 0xC1, 0xC2) and length >= 8:
            return int.from_bytes(
                data[offset + 3 : offset + 5], "big"
            ) * int.from_bytes(data[offset + 5 : offset + 7], "big")
        offset += length
    raise ExtractionError("unreadable_original")


def read_page(
    snapshot: DocumentSnapshot, page_number: int, vault_root: Path, temporary_root: Path
) -> str:
    try:
        if snapshot.media_type not in _MIME or not 1 <= page_number <= 100:
            raise ExtractionError("unsupported_original")
        data = _original(snapshot, vault_root)
        signatures = {
            "pdf": b"%PDF-",
            "png": b"\x89PNG\r\n\x1a\n",
            "jpeg": b"\xff\xd8\xff",
        }
        if not data.startswith(signatures[_MIME[snapshot.media_type]]):
            raise ExtractionError("original_mime_mismatch")
        if (
            snapshot.media_type != "application/pdf"
            and not 0 < _image_pixels(data, _MIME[snapshot.media_type]) <= MAX_PIXELS
        ):
            raise ExtractionError("page_pixel_limit")
        with pymupdf.open(stream=data, filetype=_MIME[snapshot.media_type]) as document:
            if document.is_encrypted or page_number > len(document):
                raise ExtractionError("unreadable_original")
            page = document[page_number - 1]
            text = page.get_text("text").strip()
            if text:
                if len(text) > MAX_PAGE_CHARACTERS:
                    raise ExtractionError("page_text_limit")
                return text
            scale = 2 if snapshot.media_type == "application/pdf" else 1
            width, height = page.rect.width * scale, page.rect.height * scale
            if (
                not math.isfinite(width * height)
                or width <= 0
                or height <= 0
                or math.ceil(width) * math.ceil(height) > MAX_PIXELS
            ):
                raise ExtractionError("page_pixel_limit")
            _reject_symlinks(temporary_root)
            temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary_root.chmod(0o700)
            with tempfile.TemporaryDirectory(
                prefix="lab-extraction-", dir=temporary_root
            ) as private:
                image = Path(private) / "page.png"
                image.touch(mode=0o600)
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(
                    image
                )
                image.chmod(0o600)
                text = recognize(image)
                if not text:
                    raise ExtractionError("ocr_unavailable")
                if len(text) > MAX_PAGE_CHARACTERS:
                    raise ExtractionError("page_text_limit")
                return text
    except ExtractionError:
        raise
    except Exception:  # noqa: BLE001 -- native file diagnostics must remain private
        raise ExtractionError("local_extraction_failed") from None
