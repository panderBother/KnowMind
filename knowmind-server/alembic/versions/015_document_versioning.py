"""document revisions, lifecycle and versioned chunks

Revision ID: 015_document_versioning
Revises: 014_security_message_metadata
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "015_document_versioning"
down_revision: Union[str, None] = "014_security_message_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid_type = sa.String(36, collation="utf8mb4_0900_ai_ci")
    op.add_column("documents", sa.Column("sha256", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("current_revision_id", uuid_type, nullable=True))
    op.add_column("documents", sa.Column("pending_revision_id", uuid_type, nullable=True))
    op.add_column(
        "documents",
        sa.Column("source_type", sa.String(32), nullable=False, server_default="upload"),
    )
    op.add_column("documents", sa.Column("source_key", sa.String(512), nullable=True))
    op.add_column(
        "documents",
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="active"),
    )
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "documents", sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.create_index("ix_documents_sha256", "documents", ["sha256"])
    op.create_index("ix_documents_current_revision_id", "documents", ["current_revision_id"])
    op.create_index("ix_documents_pending_revision_id", "documents", ["pending_revision_id"])
    op.create_index("ix_documents_lifecycle_status", "documents", ["lifecycle_status"])

    op.create_table(
        "document_revisions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("document_id", uuid_type, nullable=False),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("file_type", sa.String(32), nullable=True),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("file_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("extracted_text_sha256", sa.String(64), nullable=True),
        sa.Column("pipeline_fingerprint", sa.String(64), nullable=False),
        sa.Column("parsed_title", sa.String(512), nullable=True),
        sa.Column("parsed_summary", sa.String(500), nullable=True),
        sa.Column(
            "parsed_content", sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql"), nullable=True
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("parse_progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parse_stage", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("added_chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changed_chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reused_chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed_chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("document_id", "revision_no", name="uq_document_revision_no"),
    )
    op.create_index("ix_document_revisions_document_id", "document_revisions", ["document_id"])
    op.create_index("ix_document_revisions_created_by", "document_revisions", ["created_by"])
    op.create_index("ix_document_revisions_source_sha256", "document_revisions", ["source_sha256"])
    op.create_index("ix_document_revisions_status", "document_revisions", ["status"])

    op.drop_constraint("uq_document_chunk_ordinal", "document_chunks", type_="unique")
    op.add_column("document_chunks", sa.Column("revision_id", uuid_type, nullable=True))
    op.add_column("document_chunks", sa.Column("metadata_hash", sa.String(64), nullable=True))
    op.add_column("document_chunks", sa.Column("vector_fingerprint", sa.String(64), nullable=True))
    op.add_column("document_chunks", sa.Column("embedding", sa.JSON(), nullable=True))
    op.add_column(
        "document_chunks",
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="published"),
    )
    op.create_foreign_key(
        "fk_document_chunks_revision_id",
        "document_chunks",
        "document_revisions",
        ["revision_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_document_chunks_revision_id", "document_chunks", ["revision_id"])
    op.create_index(
        "ix_document_chunks_vector_fingerprint", "document_chunks", ["vector_fingerprint"]
    )
    op.create_index("ix_document_chunks_lifecycle_status", "document_chunks", ["lifecycle_status"])
    op.create_unique_constraint(
        "uq_document_revision_chunk_ordinal", "document_chunks", ["revision_id", "ordinal"]
    )
    op.create_unique_constraint("uq_document_chunk_id", "document_chunks", ["chunk_id"])


def downgrade() -> None:
    op.drop_constraint("uq_document_chunk_id", "document_chunks", type_="unique")
    op.drop_constraint("uq_document_revision_chunk_ordinal", "document_chunks", type_="unique")
    op.drop_index("ix_document_chunks_lifecycle_status", table_name="document_chunks")
    op.drop_index("ix_document_chunks_vector_fingerprint", table_name="document_chunks")
    op.drop_index("ix_document_chunks_revision_id", table_name="document_chunks")
    op.drop_constraint("fk_document_chunks_revision_id", "document_chunks", type_="foreignkey")
    op.drop_column("document_chunks", "lifecycle_status")
    op.drop_column("document_chunks", "embedding")
    op.drop_column("document_chunks", "vector_fingerprint")
    op.drop_column("document_chunks", "metadata_hash")
    op.drop_column("document_chunks", "revision_id")
    op.create_unique_constraint(
        "uq_document_chunk_ordinal", "document_chunks", ["document_id", "ordinal"]
    )

    op.drop_index("ix_document_revisions_created_by", table_name="document_revisions")
    op.drop_table("document_revisions")
    op.drop_index("ix_documents_lifecycle_status", table_name="documents")
    op.drop_index("ix_documents_pending_revision_id", table_name="documents")
    op.drop_index("ix_documents_current_revision_id", table_name="documents")
    op.drop_index("ix_documents_sha256", table_name="documents")
    op.drop_column("documents", "lock_version")
    op.drop_column("documents", "deleted_at")
    op.drop_column("documents", "lifecycle_status")
    op.drop_column("documents", "source_key")
    op.drop_column("documents", "source_type")
    op.drop_column("documents", "pending_revision_id")
    op.drop_column("documents", "current_revision_id")
    op.drop_column("documents", "sha256")
