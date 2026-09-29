from __future__ import annotations

import hashlib
import json
import re

from app.core.config import get_settings

PIPELINE_SCHEMA_VERSION = "document-pipeline-v3-structured-chunks"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    """稳定化换行和段尾空白；不折叠正文内部空格，避免改变代码/表格语义。"""
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(line.rstrip() for line in value.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def metadata_hash(*, page: int, ordinal: int) -> str:
    raw = json.dumps(
        {"page": int(page), "ordinal": int(ordinal)}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pipeline_fingerprint() -> str:
    s = get_settings()
    embedding_model = (
        s.embedding_http_model
        if (s.embedding_mode or "").strip().lower() == "http"
        else s.embedding_model_id
    )
    payload = {
        "schema": PIPELINE_SCHEMA_VERSION,
        "parser": "registry-v1",
        "ocr": {
            "provider": (
                "siliconflow"
                if s.siliconflow_api_key
                else "edgefn"
                if s.edgefn_api_key and s.edgefn_vision_model
                else "tesseract"
            ),
            "model": (
                s.siliconflow_vision_model
                if s.siliconflow_api_key
                else s.edgefn_vision_model
                if s.edgefn_api_key and s.edgefn_vision_model
                else "chi_sim+eng"
            ),
        },
        "chunk": {
            "min": s.chunk_min_chars,
            "max": s.chunk_max_chars,
            "overlap": s.chunk_overlap,
            "target_tokens": s.chunk_target_tokens,
            "max_tokens": s.chunk_max_tokens,
            "overlap_tokens": s.chunk_overlap_tokens,
        },
        "embedding": {
            "mode": s.embedding_mode,
            "model": embedding_model,
            "dim": s.embedding_vector_dim,
        },
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def vector_fingerprint(content_hash: str, pipeline_hash: str | None = None) -> str:
    raw = f"{content_hash}\0{pipeline_hash or pipeline_fingerprint()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
