from pathlib import Path

import pytest

from file_writer.operations import read_document, write_markdown


def test_write_and_read_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FILE_WRITER_ALLOWED_ROOTS", str(tmp_path))
    target = tmp_path / "a.md"
    write_markdown(str(target), "# title\n")
    out = read_document(str(target))
    assert out["content"].replace("\r\n", "\n") == "# title\n"
    assert out["status"] == "read"
