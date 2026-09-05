"""Bounded JPEG/PNG validation and optional on-device Apple Vision OCR."""

from __future__ import annotations

import subprocess
import sys
import zlib
from pathlib import Path

import pymupdf

from health_agent.pdf import ExtractedPage, ExtractedPdf, ExtractionMethod

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_OCR_CHARACTERS = 100_000

# Fixed program: paths and medical text are never interpolated into executable
# code. Vision runs on-device; it does not call an external OCR service.
_VISION_SCRIPT = r"""
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
let supported = (try? request.supportedRecognitionLanguages()) ?? []
request.recognitionLanguages = ["en-US", "ru-RU"].filter { supported.contains($0) }
do {
    try VNImageRequestHandler(cgImage: image).perform([request])
    let text = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        .joined(separator: "\n")
    print(String(text.prefix(100000)))
} catch { exit(1) }
"""


def extract_image(
    path: Path, expected_media_type: str | None = None
) -> tuple[str, ExtractedPdf]:
    """Validate before decoding/persisting; OCR output always needs human review."""
    if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError("image size is not supported")
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image size is not supported")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
        _check_single_png(data)
    elif data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
        if not data.endswith(b"\xff\xd9"):
            raise ValueError("JPEG is incomplete")
    else:
        raise ValueError("image format is not supported")
    if expected_media_type is not None and expected_media_type != media_type:
        raise ValueError("image media type does not match its content")
    try:
        width, height = image_dimensions(data, media_type)
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("image dimensions or format are not supported")
        # Header probing bounds allocation; a full decode rejects truncated data.
        pixmap = pymupdf.Pixmap(data)
        if pixmap.width != width or pixmap.height != height:
            raise ValueError("image dimensions are inconsistent")
    except Exception:  # noqa: BLE001 -- native decoder diagnostics are private
        raise ValueError("image is invalid or exceeds decoding limits") from None
    text = recognize_image(path) or ""
    method: ExtractionMethod = "local_ocr" if text.strip() else "ocr_required"
    return media_type, ExtractedPdf((ExtractedPage(1, text, method),), method)


def image_dimensions(data: bytes, media_type: str) -> tuple[int, int]:
    """Probe dimensions without allocating a decoded pixel buffer."""
    if media_type == "image/png":
        if data[8:16] != b"\x00\x00\x00\rIHDR" or len(data) < 33:
            raise ValueError("PNG header is invalid")
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            break
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        if marker in (0x01, *range(0xD0, 0xD9)):
            continue
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker == 0xE2 and data[offset + 2 : offset + 6] == b"MPF\x00":
            raise ValueError("multi-picture JPEG is not supported")
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 8:
                break
            return (
                int.from_bytes(data[offset + 5 : offset + 7], "big"),
                int.from_bytes(data[offset + 3 : offset + 5], "big"),
            )
        offset += length
    raise ValueError("JPEG header is invalid or unsupported")


def _check_single_png(data: bytes) -> None:
    offset = 8
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        expected_crc = int.from_bytes(
            data[offset + 8 + length : offset + 12 + length], "big"
        )
        if zlib.crc32(data[offset + 4 : offset + 8 + length]) != expected_crc:
            raise ValueError("PNG checksum is invalid")
        offset += length + 12
        if offset > len(data) or kind == b"acTL":
            raise ValueError("invalid or multi-frame PNG is not supported")
        if kind == b"IEND":
            if offset != len(data):
                raise ValueError("trailing image data is not supported")
            return
    raise ValueError("PNG is incomplete")


def recognize_image(path: Path) -> str | None:
    """Best-effort local OCR, with safe fallback and no diagnostic text exposure."""
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/swift", "-e", _VISION_SCRIPT, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if result.returncode != 0 or len(result.stdout) > MAX_OCR_CHARACTERS + 1:
        return None
    return result.stdout.strip() or None
