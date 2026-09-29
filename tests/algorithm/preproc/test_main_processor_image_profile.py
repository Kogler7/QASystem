"""Deterministic compatibility tests for PDF image profiling."""

from __future__ import annotations

import sys
import types

from algorithm.preproc.preproc_src.main_processor import DocumentProcessor


class _Page:
    def __init__(self, objects):
        self.objects = objects

    def get_size(self):
        return (100.0, 100.0)

    def get_objects(self, *, filter):
        return self.objects


class _Pdf:
    def __init__(self, _path, page):
        self.page = page

    def __len__(self):
        return 1

    def __getitem__(self, _index):
        return self.page

    def close(self):
        pass


def _fake_pdfium(monkeypatch, obj):
    page = _Page([obj])
    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        types.SimpleNamespace(
            PdfDocument=lambda path: _Pdf(path, page),
            raw=types.SimpleNamespace(FPDF_PAGEOBJ_IMAGE=3),
        ),
    )


def test_content_image_count_prefers_get_bounds(monkeypatch):
    class Object:
        def get_bounds(self):
            return (0.0, 0.0, 80.0, 80.0)

        def get_pos(self):  # pragma: no cover - must not be called
            raise AssertionError("get_pos should not be used when get_bounds exists")

    _fake_pdfium(monkeypatch, Object())
    assert DocumentProcessor._content_image_count("sample.pdf", 1) == 1


def test_content_image_count_supports_legacy_get_pos(monkeypatch):
    class Object:
        def get_pos(self):
            return (0.0, 0.0, 80.0, 80.0)

    _fake_pdfium(monkeypatch, Object())
    assert DocumentProcessor._content_image_count("sample.pdf", 1) == 1
