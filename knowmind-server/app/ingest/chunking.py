from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.ingest.types import PageText

log = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 640
_DEFAULT_MIN_CHARS = 120
_DEFAULT_OVERLAP = 100
_DEFAULT_TARGET_TOKENS = 360
_DEFAULT_MAX_TOKENS = 560
_DEFAULT_OVERLAP_TOKENS = 64


@dataclass
class TextChunk:
    text: str
    page: int  # 0-based


def chunk_pages(
    pages: list[PageText],
    max_chars: int = _DEFAULT_MAX_CHARS,
    overlap: int = _DEFAULT_OVERLAP,
) -> list[TextChunk]:
    """按页固定长度滑窗切块（兼容/回退）。"""
    chunks: list[TextChunk] = []
    for pt in pages:
        chunks.extend(chunk_text(pt.text, page=pt.page_index, max_chars=max_chars, overlap=overlap))
    return chunks


def chunk_text(
    text: str,
    *,
    page: int = 0,
    max_chars: int = _DEFAULT_MAX_CHARS,
    overlap: int = _DEFAULT_OVERLAP,
) -> list[TextChunk]:
    """对单段文本固定长度滑窗切块。"""
    t = (text or "").strip()
    if not t:
        return []
    chunks: list[TextChunk] = []
    start = 0
    while start < len(t):
        end = min(len(t), start + max_chars)
        piece = t[start:end].strip()
        if piece:
            chunks.append(TextChunk(text=piece, page=page))
        if end >= len(t):
            break
        start = end - overlap
    return chunks


def _split_semantic_units(text: str) -> list[str]:
    """先按段落，过长段落再按句号切分为语义单元。"""
    units: list[str] = []
    for para in re.split(r"\n\s*\n+", text):
        p = para.strip()
        if not p:
            continue
        if len(p) <= 500:
            units.append(p)
            continue
        parts = re.split(r"(?<=[。！？.!?])\s*", p)
        buf = ""
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if len(buf) + len(part) <= 500:
                buf = f"{buf}{part}" if buf else part
            else:
                if buf:
                    units.append(buf)
                buf = part
        if buf:
            units.append(buf)
    return units or [text.strip()]


def _approx_token_count(text: str) -> int:
    """近似 token 计数：中文按字、英文/数字按词、标点按单 token 估算。

    这里不直接加载 embedding tokenizer，避免切块阶段额外下载/初始化大模型；
    该估算对中英文混排和代码比纯字符数稳定得多，最终 embedding 仍由模型自身分词。
    """
    return len(re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\w\s]", text or ""))


def _split_long_unit(
    unit: str,
    *,
    page: int,
    max_chars: int,
    max_tokens: int,
    overlap_tokens: int,
) -> list[TextChunk]:
    """长段落按句子打包，必要时才回退到字符滑窗。"""
    sentences = [p.strip() for p in re.split(r"(?<=[。！？.!?；;])\s*", unit) if p.strip()]
    if len(sentences) <= 1:
        return chunk_text(unit, page=page, max_chars=max_chars, overlap=_DEFAULT_OVERLAP)

    out: list[TextChunk] = []
    buf = ""
    for sentence in sentences:
        candidate = f"{buf}{sentence}" if buf else sentence
        too_long = len(candidate) > max_chars or _approx_token_count(candidate) > max_tokens
        if too_long and buf:
            out.append(TextChunk(text=buf.strip(), page=page))
            overlap = ""
            if overlap_tokens > 0:
                prior = [
                    p.strip()
                    for p in re.split(r"(?<=[。！？.!?；;])\s*", buf)
                    if p.strip()
                ]
                carried: list[str] = []
                carried_tokens = 0
                for previous in reversed(prior):
                    count = _approx_token_count(previous)
                    if carried and carried_tokens + count > overlap_tokens:
                        break
                    carried.insert(0, previous)
                    carried_tokens += count
                overlap = "".join(carried)
            buf = f"{overlap} {sentence}".strip() if overlap else sentence
        else:
            buf = candidate

        if len(buf) > max_chars or _approx_token_count(buf) > max_tokens:
            # 单句自身超限时使用稳定滑窗，避免丢失内容。
            if out and out[-1].text == buf:
                continue
            out.extend(chunk_text(buf, page=page, max_chars=max_chars, overlap=_DEFAULT_OVERLAP))
            buf = ""

    if buf.strip():
        out.append(TextChunk(text=buf.strip(), page=page))
    return out


def _pack_units(
    units: list[str],
    *,
    min_chars: int,
    max_chars: int,
    page: int,
    overlap: int,
    target_tokens: int | None = None,
    max_tokens: int | None = None,
    overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS,
) -> list[TextChunk]:
    """
    按语义单元打包：同时受字符硬上限和 token 预算约束，短碎片仅与相邻内容合并。
    """
    chunks: list[TextChunk] = []
    buf = ""

    def flush_buffer() -> None:
        nonlocal buf
        piece = buf.strip()
        if piece:
            chunks.append(TextChunk(text=piece, page=page))
        buf = ""

    for unit in units:
        u = unit.strip()
        if not u:
            continue
        if len(u) > max_chars or (max_tokens is not None and _approx_token_count(u) > max_tokens):
            flush_buffer()
            chunks.extend(
                _split_long_unit(
                    u,
                    page=page,
                    max_chars=max_chars,
                    max_tokens=max_tokens or _DEFAULT_MAX_TOKENS,
                    overlap_tokens=overlap_tokens,
                )
            )
            continue
        candidate = f"{buf}\n\n{u}" if buf else u
        over_max = len(candidate) > max_chars or (
            max_tokens is not None and _approx_token_count(candidate) > max_tokens
        )
        if over_max:
            flush_buffer()
            buf = u
        else:
            buf = candidate
        # token 目标只控制常规块，不会把单独的标题/短列表强行切碎。
        reached_target = target_tokens is not None and _approx_token_count(buf) >= target_tokens
        if len(buf) >= min_chars and (target_tokens is None or reached_target):
            flush_buffer()

    tail = buf.strip()
    if tail:
        if chunks and len(tail) < min_chars:
            merged = f"{chunks[-1].text}\n\n{tail}"
            if len(merged) <= max_chars and (
                max_tokens is None or _approx_token_count(merged) <= max_tokens
            ):
                chunks[-1] = TextChunk(text=merged, page=page)
            else:
                chunks.append(TextChunk(text=tail, page=page))
        else:
            chunks.append(TextChunk(text=tail, page=page))

    return chunks


def semantic_chunk_text(
    text: str,
    *,
    page: int = 0,
    max_chars: int = _DEFAULT_MAX_CHARS,
    min_chars: int = _DEFAULT_MIN_CHARS,
    overlap: int = _DEFAULT_OVERLAP,
    breakpoint_percentile: float = 25.0,  # noqa: ARG001 — 保留签名兼容
    target_tokens: int | None = None,
    max_tokens: int | None = None,
    overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS,
) -> list[TextChunk]:
    """
    结构优先切块：段落/句号切分 → token 预算打包 → 必要时字符滑窗兜底。
    不再按嵌入相似度合并到超大块。
    """
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= max_chars and (max_tokens is None or _approx_token_count(t) <= max_tokens):
        return [TextChunk(text=t, page=page)]

    units = _split_semantic_units(t)
    if len(units) <= 1:
        return chunk_text(t, page=page, max_chars=max_chars, overlap=overlap)

    packed = _pack_units(
        units,
        min_chars=min_chars,
        max_chars=max_chars,
        page=page,
        overlap=overlap,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )
    return packed if packed else chunk_text(t, page=page, max_chars=max_chars, overlap=overlap)


def semantic_chunk_pages(
    pages: list[PageText],
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    min_chars: int = _DEFAULT_MIN_CHARS,
    overlap: int = _DEFAULT_OVERLAP,
    target_tokens: int | None = None,
    max_tokens: int | None = None,
    overlap_tokens: int = _DEFAULT_OVERLAP_TOKENS,
) -> list[TextChunk]:
    """多页文档：按页语义切块。"""
    chunks: list[TextChunk] = []
    for pt in pages:
        t = (pt.text or "").strip()
        if not t:
            continue
        chunks.extend(
            semantic_chunk_text(
                t,
                page=pt.page_index,
                max_chars=max_chars,
                min_chars=min_chars,
                overlap=overlap,
                target_tokens=target_tokens,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            ),
        )
    return chunks


def chunk_settings_from_config() -> tuple[int, int, int]:
    """从运行时配置读取切块参数。"""
    from app.core.config import get_settings

    s = get_settings()
    return (
        int(s.chunk_min_chars),
        int(s.chunk_max_chars),
        int(s.chunk_overlap),
    )


def chunk_token_settings_from_config() -> tuple[int, int, int]:
    """读取 token 级切块参数；旧配置不存在时使用稳定默认值。"""
    from app.core.config import get_settings

    s = get_settings()
    return (
        int(getattr(s, "chunk_target_tokens", _DEFAULT_TARGET_TOKENS)),
        int(getattr(s, "chunk_max_tokens", _DEFAULT_MAX_TOKENS)),
        int(getattr(s, "chunk_overlap_tokens", _DEFAULT_OVERLAP_TOKENS)),
    )
