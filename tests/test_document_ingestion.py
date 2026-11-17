"""Tests for PIX-4704 document ingestion (foresight.document_ingestion)."""

from __future__ import annotations

import builtins
import importlib

import pytest

from foresight.document_ingestion import (
    DocumentIngestionError,
    DocumentIngestOptions,
    detect_document_kind,
    extract_text,
    ingest_document_file,
)
from foresight.rest_api import _ROUTES


def _block_imports(monkeypatch, *names: str) -> None:
    """Make ``names`` unimportable by both import mechanisms the code uses.

    ``foresight.document_ingestion`` binds ``pypdf`` through a plain
    ``from pypdf import ...`` and ``pytesseract`` through
    ``importlib.import_module`` at its guarded call site.
    ``importlib.import_module`` resolves the top-level module through
    ``importlib._bootstrap`` rather than ``builtins.__import__``, so patching
    only the latter does not block it at lookup time. It happens to fail today
    anyway, but only because pytesseract's own package-internal imports route
    through ``builtins.__import__`` — an implementation detail of the
    dependency, not a contract this test should lean on. Both entry points are
    patched, and each delegates to the real implementation for every other
    module so unrelated lazy imports keep working.
    """
    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def _root(name: str) -> str:
        return name.split(".", maxsplit=1)[0]

    def _blocked(name: str) -> ImportError:
        return ImportError(f"blocked optional dependency: {_root(name)}")

    def fake_import(name, *args, **kwargs):
        if _root(name) in names:
            raise _blocked(name)
        return real_import(name, *args, **kwargs)

    def fake_import_module(name, *args, **kwargs):
        if _root(name) in names:
            raise _blocked(name)
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(importlib, "import_module", fake_import_module)


def test_detect_document_kind():
    assert detect_document_kind("report.pdf") == "pdf"
    assert detect_document_kind("scan.PDF") == "pdf"
    assert detect_document_kind("photo.png") == "image"
    assert detect_document_kind("photo.JPEG") == "image"
    assert detect_document_kind("notes.txt") == "text"
    assert detect_document_kind("notes.md") == "text"
    assert detect_document_kind("no-extension") == "text"


def test_extract_text_plain(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello world\nsecond line", encoding="utf-8")
    assert extract_text(f) == "hello world\nsecond line"


def test_extract_text_missing_file_raises(tmp_path):
    with pytest.raises(DocumentIngestionError, match="not found"):
        extract_text(tmp_path / "nope.pdf")


def test_pdf_missing_dependency(tmp_path, monkeypatch):
    _block_imports(monkeypatch, "pypdf")
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(DocumentIngestionError, match=r"foresight\[pdf\]"):
        extract_text(f)


def test_ocr_missing_dependency(tmp_path, monkeypatch):
    _block_imports(monkeypatch, "pytesseract")
    f = tmp_path / "scan.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(DocumentIngestionError, match=r"foresight\[ocr\]"):
        extract_text(f)


def test_ingest_document_file_text(tmp_path, monkeypatch):
    f = tmp_path / "journal.txt"
    f.write_text("captured thought", encoding="utf-8")

    captured: dict = {}

    def fake_create_document(params):
        captured.update(params.model_dump())
        return '{"document": {"id": "d1"}}'

    monkeypatch.setattr("foresight.server.create_document", fake_create_document)

    result = ingest_document_file(str(f))
    assert captured["title"] == "journal"
    assert captured["content"] == "captured thought"
    assert captured["source"] == "document"
    assert result == '{"document": {"id": "d1"}}'


def test_ingest_document_file_pdf_source_default(tmp_path, monkeypatch):
    f = tmp_path / "paper.pdf"
    f.write_text("placeholder", encoding="utf-8")

    captured: dict = {}

    def fake_extract(path):
        captured["kind"] = detect_document_kind(path)
        return "extracted text"

    def fake_create_document(params):
        captured.update(params.model_dump())
        return '{"document": {}}'

    monkeypatch.setattr("foresight.document_ingestion.extract_text", fake_extract)
    monkeypatch.setattr("foresight.server.create_document", fake_create_document)

    ingest_document_file(str(f), DocumentIngestOptions(title="My Paper"))
    assert captured["kind"] == "pdf"
    assert captured["title"] == "My Paper"
    assert captured["source"] == "pdf"


def test_rest_route_registered():

    assert any(path == "/documents/ingest" for path, *_ in _ROUTES)
