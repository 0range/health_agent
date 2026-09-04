"""Extraction of embedded text from PDF documents."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf

ExtractionMethod = Literal["digital_text", "ocr_required"]


@dataclass(frozen=True)
class ExtractedPage:
    """Text extracted from one-based PDF page number."""

    page_number: int
    text: str
    extraction_method: ExtractionMethod = "digital_text"


@dataclass(frozen=True)
class ExtractedPdf:
    """The embedded-text extraction result for a PDF document."""

    pages: tuple[ExtractedPage, ...]
    extraction_method: ExtractionMethod


def extract_pdf(path: Path) -> ExtractedPdf:
    """Extract embedded page text, leaving image-only documents for later OCR."""
    with pymupdf.open(path) as document:
        pages = tuple(
            ExtractedPage(
                page_number=index + 1,
                text=text,
                extraction_method="digital_text" if text.strip() else "ocr_required",
            )
            for index, page in enumerate(document)
            for text in (page.get_text("text"),)
        )

    extraction_method: ExtractionMethod = (
        "digital_text" if any(page.text.strip() for page in pages) else "ocr_required"
    )
    return ExtractedPdf(pages=pages, extraction_method=extraction_method)
