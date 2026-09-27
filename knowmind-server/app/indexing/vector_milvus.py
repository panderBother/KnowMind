from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class MilvusRemoteIndex:
    """远程 Milvus（如 Docker `localhost:19530`）。"""

    def __init__(self, uri: str, *, collection_name: str, vector_dim: int) -> None:
        from pymilvus import MilvusClient

        self._c = MilvusClient(uri=uri)
        self._name = collection_name
        self._dim = vector_dim
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        from pymilvus import CollectionSchema, DataType, FieldSchema

        if self._c.has_collection(self._name):
            description = self._c.describe_collection(collection_name=self._name)
            field_names = {
                str(field.get("name") or "")
                for field in description.get("fields", [])
                if isinstance(field, dict)
            }
            required = {
                "chunk_id",
                "kb_id",
                "user_id",
                "doc_id",
                "revision_id",
                "item_id",
                "lifecycle_status",
                "page",
                "text",
                "vector",
            }
            missing = required - field_names
            if missing:
                raise RuntimeError(
                    f"Milvus collection {self._name} schema is outdated; "
                    f"missing fields: {', '.join(sorted(missing))}"
                )
            return
        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="revision_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="item_id", dtype=DataType.VARCHAR, max_length=36),
            FieldSchema(name="lifecycle_status", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="page", dtype=DataType.INT64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=16384),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=int(self._dim)),
        ]
        schema = CollectionSchema(fields)
        self._c.create_collection(collection_name=self._name, schema=schema)
        log.info("milvus collection created: %s dim=%s", self._name, self._dim)

    def upsert_chunks(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        data = [
            {
                "chunk_id": r["chunk_id"],
                "kb_id": r["kb_id"],
                "user_id": r["user_id"],
                "doc_id": r["doc_id"],
                "revision_id": str(r.get("revision_id") or ""),
                "item_id": str(r.get("item_id") or ""),
                "lifecycle_status": str(r.get("lifecycle_status") or "published"),
                "page": int(r["page"]),
                "text": str(r["text"])[:16300],
                "vector": r["vector"],
            }
            for r in rows
        ]
        self._c.upsert(collection_name=self._name, data=data)
        log.info("milvus upsert %s chunks", len(rows))

    def delete_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        try:
            self._c.delete(collection_name=self._name, ids=chunk_ids)
        except Exception as e:
            log.warning("milvus delete failed: %s", e)

    def list_chunk_ids_for_doc(self, doc_id: str) -> list[str]:
        if not doc_id:
            return []
        try:
            rows = self._c.query(
                collection_name=self._name,
                filter=f'doc_id == "{doc_id}"',
                output_fields=["chunk_id"],
                limit=16384,
            )
        except Exception as e:
            log.warning("milvus list by doc_id failed: %s", e)
            return []
        return [str(row.get("chunk_id") or "") for row in rows or [] if row.get("chunk_id")]

    def query_similar(
        self,
        *,
        kb_id: str,
        query_embedding: list[float],
        top_k: int,
    ) -> list[dict[str, Any]]:
        k = max(1, min(top_k, 64))
        try:
            res = self._c.search(
                collection_name=self._name,
                data=[query_embedding],
                filter=f'kb_id == "{kb_id}" and lifecycle_status == "published"',
                limit=k,
                output_fields=[
                    "chunk_id",
                    "text",
                    "doc_id",
                    "revision_id",
                    "item_id",
                    "page",
                ],
                search_params={"metric_type": "COSINE"},
            )
        except Exception as e:
            log.warning("milvus search failed: %s", e)
            return []

        out: list[dict[str, Any]] = []
        for hit_list in res or []:
            for hit in hit_list:
                try:
                    dist_raw = (
                        hit["distance"] if isinstance(hit, dict) else getattr(hit, "distance", None)
                    )
                    ent_raw = hit["entity"] if isinstance(hit, dict) else getattr(hit, "entity", {})
                except (KeyError, TypeError):
                    continue
                ent: dict[str, Any]
                if isinstance(ent_raw, dict):
                    ent = ent_raw
                else:
                    try:
                        ent = dict(ent_raw)
                    except Exception:
                        continue
                dist = float(dist_raw) if dist_raw is not None else None
                out.append(
                    {
                        "chunk_id": ent.get("chunk_id"),
                        "text": str(ent.get("text") or ""),
                        "doc_id": str(ent.get("doc_id") or ""),
                        "revision_id": str(ent.get("revision_id") or ""),
                        "item_id": str(ent.get("item_id") or ""),
                        "page": int(ent.get("page") or 0),
                        "distance": dist,
                    },
                )
        return out
