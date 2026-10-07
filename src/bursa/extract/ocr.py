"""OCR fallback for scanned PDF pages.

When PyMuPDF yields no extractable text from a page (a scanned image), this
module renders the page to a bitmap and runs Tesseract OCR to recover words
with bounding boxes — the same (text, x0, y0, x1, y1) shape that
``layout.words_from_page`` expects, so the rest of the extraction pipeline
is unchanged.

Requirements:
  pip install pytesseract pdf2image Pillow
  System: Tesseract-OCR binary + Poppler (pdftopm) on PATH.
"""

from __future__ import annotations

from pathlib import Path

from bursa.extract.layout import Word


def ocr_page_words(pdf_path: Path, page_no: int, dpi: int = 300) -> list[Word]:
    """Render one 1-indexed page to an image, OCR it, return Word objects.

    Coordinates are scaled back to PDF points (72 dpi) so they align with
    PyMuPDF's coordinate space.
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError as exc:
        raise ImportError(
            "OCR requires pytesseract and pdf2image. "
            "Install with: pip install pytesseract pdf2image Pillow"
        ) from exc

    images = convert_from_path(
        str(pdf_path),
        dpi=dpi,
        first_page=page_no,
        last_page=page_no,
    )
    if not images:
        return []

    img = images[0]
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    scale = 72.0 / dpi
    words: list[Word] = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        conf = int(data["conf"][i])
        if conf < 30:
            continue
        x = data["left"][i] * scale
        y = data["top"][i] * scale
        w = data["width"][i] * scale
        h = data["height"][i] * scale
        words.append(Word(text=text, x0=x, y0=y, x1=x + w, y1=y + h))

    return words


def page_needs_ocr(page) -> bool:  # type: ignore[no-untyped-def]
    """Whether a PyMuPDF page has no extractable text (scanned image)."""
    raw = page.get_text("words")
    return len(raw) < 5
