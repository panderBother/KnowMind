
from arxiv.operations import extract_arxiv_ids, format_arxiv_markdown


def test_extract_arxiv_ids() -> None:
    text = "请读 https://arxiv.org/abs/2301.07041 和 1706.03762"
    ids = extract_arxiv_ids(text)
    assert "2301.07041" in ids
    assert "1706.03762" in ids


def test_format_arxiv_markdown_empty() -> None:
    md = format_arxiv_markdown({"items": []})
    assert "未找到" in md


def test_format_arxiv_markdown_rate_limit() -> None:
    md = format_arxiv_markdown({"items": [], "error": "429 Too Many Requests"})
    assert "过于频繁" in md
