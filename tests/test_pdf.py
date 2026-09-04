from pathlib import Path

import pymupdf
import pytest

from health_agent.pdf import extract_pdf


@pytest.fixture
def synthetic_lab_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "labs.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Ferritin 42 ng/mL 30-400")
    document.save(path)
    document.close()
    return path


def test_extract_pdf_preserves_page_number_and_text(synthetic_lab_pdf: Path) -> None:
    result = extract_pdf(synthetic_lab_pdf)

    assert result.pages[0].page_number == 1
    assert "Ferritin" in result.pages[0].text
    assert result.extraction_method == "digital_text"


def test_extract_pdf_marks_scanned_documents_for_ocr(tmp_path: Path) -> None:
    path = tmp_path / "scanned.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()

    result = extract_pdf(path)

    assert result.pages[0].text == ""
    assert result.extraction_method == "ocr_required"
