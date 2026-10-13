"""Document ingestion for PDF and image (OCR) sources (PIX-4704).

The document layer (:mod:`foresight.document_layer`) already stores and chunks
plain text. This module adds the file-facing front door: it turns a local PDF
or image file into text and hands it to ``create_document``.

Two optional capabilities, both **opt-in and fully local** (no network egress):

* **PDF text extraction** via ``pypdf`` — ``pip install 'foresight[pdf]'``.
* **OCR** of raster images via ``pytesseract`` + ``Pillow`` (requires the
  ``tesseract`` binary on the host) — ``pip install 'foresight[ocr]'``.

When a capability's dependency is missing, ingestion raises
:class:`DocumentIngestionError` with an actionable install hint instead of
silently degrading.

Scanned PDFs (image-only, no text layer) are not rasterised here; OCR them to
images first, then ingest the images.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_PDF_EXTENSIONS = {".pdf"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_TEXT_EXTENSIONS = {".txt", ".md", ".text", ".json", ".csv", ".log", ".yaml", ".yml"}


class DocumentIngestionError(ValueError):
    """Raised when a document file cannot be read, parsed, or OCR'd."""


def detect_document_kind(path: str | Path) -> str:
    """Return ``"pdf"``, ``"image"``, or ``"text"`` for a file path."""
    suffix = Path(path).suffix.lower()
    if suffix in _PDF_EXTENSIONS:
        return "pdf"
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    return "text"


def _require_file(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_file():
        raise DocumentIngestionError(f"document file not found: {p}")
    return p


def extract_text(path: str | Path) -> str:
    """Extract raw text from a PDF, image (OCR), or plain-text file."""
    p = _require_file(path)
    kind = detect_document_kind(p)
    if kind == "pdf":
        return _extract_pdf_text(p)
    if kind == "image":
        return _extract_ocr_text(p)
    return _read_text(p)


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency optional
        raise DocumentIngestionError(
            "PDF ingestion requires the optional 'pypdf' dependency; install it with: pip install 'foresight[pdf]'"
        ) from exc
    try:
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # pragma: no cover - malformed/encrypted PDFs
        raise DocumentIngestionError(f"failed to read PDF {path.name}: {exc}") from exc
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        raise DocumentIngestionError(
            f"no extractable text in {path.name}; it may be a scanned (image-only) "
            "PDF. OCR its pages to images first, then ingest the images."
        )
    return text


def _extract_ocr_text(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency optional
        raise DocumentIngestionError(
            "OCR ingestion requires the optional 'pytesseract' + 'Pillow' "
            "dependencies; install them with: pip install 'foresight[ocr]' "
            "(and ensure the 'tesseract' binary is on your PATH)"
        ) from exc
    try:
        return pytesseract.image_to_string(Image.open(str(path))).strip()
    except Exception as exc:  # pragma: no cover - tesseract missing / bad image
        raise DocumentIngestionError(
            f"OCR failed for {path.name}: {exc}. Confirm the 'tesseract' binary is installed and on PATH."
        ) from exc


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise DocumentIngestionError(f"{path.name} is not valid UTF-8 text; provide a PDF or image instead.") from exc


def ingest_document_file(
    path: str,
    title: str | None = None,
    user_id: str | None = None,
    source: str | None = None,
    char_budget: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Extract text from a local PDF/image/text file and store it as a document.

    Args:
        path: Local filesystem path to the file (self-hosted deployments).
        title: Human-readable title; defaults to the file stem.
        user_id: Optional user ID override.
        source: Document source type; defaults to ``"pdf"`` for PDFs, else ``"document"``.
        char_budget: Optional soft max chars per chunk (passed to ``create_document``).
        metadata: Optional JSON-serializable metadata.

    Returns:
        The JSON string produced by ``create_document``.
    """
    p = _require_file(path)
    kind = detect_document_kind(p)
    content = extract_text(p)
    if not content:
        raise DocumentIngestionError(f"no text extracted from {p.name}")

    from .server import create_document

    resolved_title = title or p.stem or p.name
    resolved_source = source or ("pdf" if kind == "pdf" else "document")
    kwargs: dict[str, Any] = {
        "title": resolved_title,
        "content": content,
        "user_id": user_id,
        "source": resolved_source,
        "metadata": metadata,
    }
    if char_budget is not None:
        kwargs["char_budget"] = char_budget
    return create_document(**kwargs)


def ingest_document_file_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Like :func:`ingest_document_file` but returns parsed JSON (convenience)."""
    return json.loads(ingest_document_file(*args, **kwargs))
