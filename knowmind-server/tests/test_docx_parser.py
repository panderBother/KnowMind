from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from app.ingest.docx_parser import (
    _parse_docx_xml_fallback,
    parse_docx,
    strip_null_relationships,
)

_DOC_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>章节测验第一题</w:t></w:r></w:p>
    <w:p><w:r><w:t>第二段内容</w:t></w:r></w:p>
  </w:body>
</w:document>"""

_RELS_WITH_NULL = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="NULL"/>
</Relationships>"""

_RELS_FIXED = strip_null_relationships(_RELS_WITH_NULL)


def _write_broken_docx(path: Path) -> None:
    content_types = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    root_rels = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", _DOC_XML.encode("utf-8"))
        zf.writestr("word/_rels/document.xml.rels", _RELS_WITH_NULL)


def test_strip_null_relationships() -> None:
    assert b'Target="NULL"' not in _RELS_FIXED
    assert b"word/document.xml" in _RELS_FIXED


def test_parse_docx_xml_fallback() -> None:
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
        path = Path(f.name)
    try:
        _write_broken_docx(path)
        text = _parse_docx_xml_fallback(str(path))
        assert "章节测验第一题" in text
        assert "第二段内容" in text
    finally:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            pass


def test_parse_docx_after_repair() -> None:
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
        path = Path(f.name)
    try:
        _write_broken_docx(path)
        result = parse_docx(str(path), "章节测验.docx")
        assert "章节测验第一题" in result.merged_content()
    finally:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            pass
