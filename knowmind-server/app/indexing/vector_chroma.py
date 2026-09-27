from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ChromaVectorIndex:
    """Chroma 持久化（跨平台）；metadata 用于按 kb_id 过滤。"""

    def __init__(self, data_path: str | Path, *, collection_name: str) -> None:
        import chromadb
        from chromadb.config import Settings

        root = Path(data_path)
        root.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(root.resolve()),
            settings=Settings(anonymized_telemetry=False),
        )
        self._col = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert_chunks(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        ids = [r["chunk_id"] for r in rows]
        embeddings = [r["vector"] for r in rows]
        documents = [str(r["text"])[:16000] for r in rows]
        metadatas = [
            {
                "kb_id": r["kb_id"],
                "user_id": r["user_id"],
                "doc_id": str(r.get("doc_id") or ""),
                "revision_id": str(r.get("revision_id") or ""),
                "item_id": str(r.get("item_id") or ""),
                "page": int(r["page"]),
                "lifecycle_status": str(r.get("lifecycle_status") or "published"),
            }
            for r in rows
        ]
        self._col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)
        log.info("chroma upsert %s chunks", len(rows))

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        try:
            self._col.delete(ids=chunk_ids)
            log.info("chroma delete %s chunks", len(chunk_ids))
        except Exception as e:
            log.warning("chroma delete failed: %s", e)

    def delete_chunks_for_doc(self, doc_id: str) -> None:
        if not doc_id:
            return
        try:
            self._col.delete(where={"doc_id": doc_id})
            log.info("chroma delete where doc_id=%s", doc_id)
        except Exception as e:
            log.warning("chroma delete by doc_id failed: %s", e)

    def delete_chunks_for_item(self, item_id: str) -> None:
        if not item_id:
            return
        try:
            self._col.delete(where={"item_id": item_id})
            log.info("chroma delete where item_id=%s", item_id)
        except Exception as e:
            log.warning("chroma delete by item_id failed: %s", e)

    def list_chunk_ids_for_doc(self, doc_id: str) -> list[str]:
        if not doc_id:
            return []
        try:
            raw = self._col.get(where={"doc_id": doc_id}, include=[])
        except Exception as e:
            log.warning("chroma list by doc_id failed: %s", e)
            return []
        ids = raw.get("ids") if isinstance(raw, dict) else None
        if not ids:
            return []
        if ids and isinstance(ids[0], list):
            return [str(x) for x in ids[0] if x]
        return [str(x) for x in ids if x]

    def query_similar(
        self,
        *,
        kb_id: str,
        query_embedding: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        k = max(1, min(top_k, 64))
        try:
            raw = self._col.query(
                query_embeddings=[query_embedding],
                n_results=k,
                where={
                    "$and": [
                        {"kb_id": kb_id},
                        {"lifecycle_status": "published"},
                    ]
                },
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            log.warning("chroma query failed: %s", e)
            return []

        ids_list = raw.get("ids") or []
        if not ids_list or not ids_list[0]:
            try:
                raw = self._col.query(
                    query_embeddings=[query_embedding],
                    n_results=k,
                    where={"kb_id": kb_id},
                    include=["documents", "metadatas", "distances"],
                )
            except Exception as e:
                log.warning("chroma legacy query failed: %s", e)
                return []

        ids_list = raw.get("ids") or []
        docs_list = raw.get("documents") or []
        meta_list = raw.get("metadatas") or []
        dist_list = raw.get("distances") or []
        if not ids_list or not ids_list[0]:
            return []

        out: list[dict[str, Any]] = []
        for i, cid in enumerate(ids_list[0]):
            doc_txt = (docs_list[0][i] if docs_list and docs_list[0] else "") or ""
            meta = (meta_list[0][i] if meta_list and meta_list[0] else {}) or {}
            dist = None
            if dist_list and dist_list[0] and i < len(dist_list[0]):
                dist = float(dist_list[0][i])
            out.append(
                {
                    "chunk_id": cid,
                    "text": doc_txt,
                    "doc_id": str(meta.get("doc_id", "")),
                    "revision_id": str(meta.get("revision_id", "")),
                    "item_id": str(meta.get("item_id", "")),
                    "page": int(meta.get("page") or 0),
                    "distance": dist,
                },
            )
        return out
