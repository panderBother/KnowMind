from datetime import datetime

from pydantic import BaseModel, Field


class DocumentOut(BaseModel):
    id: str
    kb_id: str
    filename: str
    file_type: str | None = None
    status: str
    chunk_count: int
    file_bytes: int
    md5: str | None
    sha256: str | None = None
    current_revision_id: str | None = None
    pending_revision_id: str | None = None
    lifecycle_status: str = "active"
    title: str | None
    parsed_title: str | None = None
    parsed_summary: str | None = None
    parse_progress: int = 0
    parse_stage: str | None = None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentUploadResponse(BaseModel):
    documents: list[DocumentOut] = Field(default_factory=list)
    skipped_duplicates: int = 0
    needs_preview: list[str] = Field(default_factory=list, description="需预览确认的文档 ID")


class DocumentRevisionOut(BaseModel):
    id: str
    document_id: str
    created_by: str
    revision_no: int
    filename: str
    file_type: str | None = None
    file_bytes: int
    source_sha256: str
    pipeline_fingerprint: str
    status: str
    parse_progress: int = 0
    parse_stage: str | None = None
    error_message: str | None = None
    chunk_count: int = 0
    added_chunk_count: int = 0
    changed_chunk_count: int = 0
    reused_chunk_count: int = 0
    removed_chunk_count: int = 0
    created_at: datetime
    activated_at: datetime | None = None
    is_current: bool = False

    model_config = {"from_attributes": True}


class DocumentVersionUploadResponse(BaseModel):
    document: DocumentOut
    revision: DocumentRevisionOut | None = None
    unchanged: bool = False


class DocumentIndexReconcileResponse(BaseModel):
    expected_chunks: int = 0
    vector_chunks: int = 0
    keyword_chunks: int = 0
    repaired_chunks: int = 0
    removed_orphans: int = 0
    unrecoverable_chunks: int = 0


class DocumentParsedContentOut(BaseModel):
    doc_id: str
    filename: str
    file_type: str | None
    title: str | None
    summary: str | None
    content: str
    status: str


class DocumentParsedContentUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=512)
    summary: str | None = Field(default=None, max_length=500)
    content: str = Field(..., min_length=1, max_length=200_000)


class DocumentConfirmImportResponse(BaseModel):
    document: DocumentOut
