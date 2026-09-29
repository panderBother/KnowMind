from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# MySQL TEXT 仅 ~64KB；大文档/条目用 MEDIUMTEXT（SQLite 测试仍用 Text）
MediumText = Text().with_variant(MEDIUMTEXT(), "mysql")


def new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    token_version: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    knowledge_bases: Mapped[list[KnowledgeBase]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
    )


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    owner: Mapped[User] = relationship(back_populates="knowledge_bases")
    documents: Mapped[list[Document]] = relationship(
        "Document",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )
    categories: Mapped[list[KnowledgeCategory]] = relationship(
        "KnowledgeCategory",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )
    items: Mapped[list[KnowledgeItem]] = relationship(
        "KnowledgeItem",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )


class KnowledgeCategory(Base):
    __tablename__ = "knowledge_categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_categories.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="categories")
    parent: Mapped[KnowledgeCategory | None] = relationship(
        remote_side="KnowledgeCategory.id",
        back_populates="children",
    )
    children: Mapped[list[KnowledgeCategory]] = relationship(back_populates="parent")
    items: Mapped[list["KnowledgeItem"]] = relationship(back_populates="category")


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="SET NULL"), index=True, nullable=True
    )
    category_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_categories.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(MediumText, nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft", index=True
    )
    access_level: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")
    source: Mapped[str | None] = mapped_column(String(512), nullable=True)
    chunk_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="items")
    category: Mapped[KnowledgeCategory | None] = relationship(back_populates="items")
    document: Mapped[Document | None] = relationship(back_populates="items")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    file_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    md5: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    pending_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="upload", server_default="upload"
    )
    source_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active", index=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parsed_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parsed_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    parsed_content: Mapped[str | None] = mapped_column(MediumText, nullable=True)
    parse_progress: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    parse_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    items: Mapped[list[KnowledgeItem]] = relationship(back_populates="document")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    revisions: Mapped[list[DocumentRevision]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentRevision.revision_no",
    )


class DocumentRevision(Base):
    """不可变的文档文件版本；成功激活后由 Document.current_revision_id 指向。"""

    __tablename__ = "document_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "revision_no", name="uq_document_revision_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    source_sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    extracted_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pipeline_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    parsed_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parsed_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    parsed_content: Mapped[str | None] = mapped_column(MediumText, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending", index=True
    )
    parse_progress: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    parse_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    added_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    changed_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reused_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    removed_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    document: Mapped[Document] = relationship(back_populates="revisions")
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
    )


class DocumentChunk(Base):
    """文档切块清单，用于内容 Hash 对比和索引差量更新。"""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("revision_id", "ordinal", name="uq_document_revision_chunk_ordinal"),
        UniqueConstraint("chunk_id", name="uq_document_chunk_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("document_revisions.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    chunk_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    metadata_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="published", server_default="published", index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="chunks")
    revision: Mapped[DocumentRevision | None] = relationship(back_populates="chunks")


class Conversation(Base):
    """多轮对话会话（MySQL 事实源）。"""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    knowledge_base_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True
    )
    expert_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("expert_agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parent_conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    branched_from_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_pinned: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default="0", index=True
    )
    model_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="balanced", server_default="balanced"
    )
    deep_research: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    web_search: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_summarized_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_summary_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acc_turns_since_summary: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0"
    )
    acc_tokens_since_summary: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    owner: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    summaries: Mapped[list[ConversationSummary]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    working_facts: Mapped[list[ConversationFact]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_conversation_sequence", "conversation_id", "sequence_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    token_est: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    sequence_no: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0"
    )
    generation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="completed", server_default="completed"
    )
    reply_to_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    citations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    attachments_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_traces_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AiUsageEvent(Base):
    """一次对话生成的估算账本；不包含规划、嵌入及工具循环的全部上游用量。"""

    __tablename__ = "ai_usage_events"
    __table_args__ = (
        Index("ix_ai_usage_user_created", "user_id", "created_at"),
        Index("ix_ai_usage_trace", "trace_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="edgefn")
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    model_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="balanced")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    estimated_cost_microusd: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, default=0, server_default="0"
    )
    latency_ms: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    covers_up_to_message_id: Mapped[str] = mapped_column(String(36), nullable=False)
    summary_text: Mapped[str] = mapped_column(Text(), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="summaries")


class ConversationFact(Base):
    """LLM 从对话中提取的可更新工作记忆事实。"""

    __tablename__ = "conversation_facts"
    __table_args__ = (UniqueConstraint("conversation_id", "fact_key", name="uq_conv_fact_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    fact_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_value: Mapped[str] = mapped_column(Text(), nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.8, server_default="0.8")
    source_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[Conversation] = relationship(back_populates="working_facts")


class RagRetrievalLog(Base):
    __tablename__ = "rag_retrieval_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    avg_score: Mapped[float | None] = mapped_column(nullable=True)
    hit_scores: Mapped[list | None] = mapped_column(JSON, nullable=True)
    top_item_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserFeedback(Base):
    __tablename__ = "user_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    correction: Mapped[str] = mapped_column(Text, nullable=False)
    topic_key: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeGap(Base):
    __tablename__ = "knowledge_gaps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    gap_key: Mapped[str] = mapped_column(String(256), nullable=False)
    trigger_rule: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_queries: Mapped[list] = mapped_column(JSON, nullable=False)
    avg_score: Mapped[float | None] = mapped_column(nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    draft_item_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class KnowledgeUsageEvent(Base):
    """知识使用热度事件：检索命中、RAG 引用、对话轮次。"""

    __tablename__ = "knowledge_usage_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("knowledge_items.id", ondelete="SET NULL"), nullable=True
    )
    document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ResearchReport(Base):
    """会话一键生成的结构化研究报告。"""

    __tablename__ = "research_reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    raw_answer_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    outline_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    citations_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ready", server_default="ready"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ExpertAgent(Base):
    """按知识库一键生成的领域专家 Agent。"""

    __tablename__ = "expert_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
