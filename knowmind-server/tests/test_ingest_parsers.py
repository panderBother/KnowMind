from __future__ import annotations

import tempfile
from pathlib import Path


from app.ingest.registry import detect_file_type, parse_file, requires_preview
from app.ingest.types import FileType


def test_detect_file_types() -> None:
    assert detect_file_type("a.PDF") == FileType.PDF
    assert detect_file_type("b.docx") == FileType.DOCX
    assert detect_file_type("c.csv") == FileType.CSV
    assert detect_file_type("d.png") == FileType.IMAGE
    assert requires_preview(FileType.PDF) is True
    assert requires_preview(FileType.DOCX) is True


def test_parse_markdown_text() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write("# Hello\n\nParagraph one.")
        path = f.name
    try:
        result = parse_file(path)
        assert "Hello" in result.merged_content()
        assert result.title == Path(path).stem
    finally:
        Path(path).unlink(missing_ok=True)


def test_parse_csv_data_dictionary() -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
        f.write("name,age\nAlice,30\nBob,25\n")
        path = f.name
    try:
        result = parse_file(path)
        content = result.merged_content()
        assert "字段说明" in content
        assert "Alice" in content
        assert result.summary
    finally:
        Path(path).unlink(missing_ok=True)
