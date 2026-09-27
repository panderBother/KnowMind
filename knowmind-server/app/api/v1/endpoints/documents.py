from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id
from app.db.session import get_db
from app.schemas.document import (
    DocumentConfirmImportResponse,
    DocumentIndexReconcileResponse,
    DocumentOut,
    DocumentParsedContentOut,
    DocumentParsedContentUpdate,
    DocumentRevisionOut,
    DocumentUploadResponse,
    DocumentVersionUploadResponse,
)
from app.services import document_service
from app.services.rate_limit_service import enforce_upload_limits

router = APIRouter()

SUPPORTED_FORMATS_HINT = "PDF、DOCX/DOC、Excel/CSV、Markdown/TXT、PNG/JPG 等图片"


@router.get("/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    kb_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    rows = await document_service.list_documents(session, user_id, kb_id)
    return [DocumentOut.model_validate(r) for r in rows]


@router.post("/{kb_id}/documents", response_model=DocumentUploadResponse)
async def upload_documents(
    kb_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
    files: list[UploadFile] = File(...),
):
    await enforce_upload_limits(user_id)
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请选择至少一个文件")
    return await document_service.upload_documents(session, user_id, kb_id, files, background_tasks)


@router.get("/{kb_id}/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    kb_id: str,
    doc_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    doc = await document_service.get_document(session, user_id, kb_id, doc_id)
    return DocumentOut.model_validate(doc)


@router.get("/{kb_id}/documents/{doc_id}/parsed-content", response_model=DocumentParsedContentOut)
async def get_document_parsed_content(
    kb_id: str,
    doc_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.get_parsed_content(session, user_id, kb_id, doc_id)


@router.put("/{kb_id}/documents/{doc_id}/parsed-content", response_model=DocumentParsedContentOut)
async def update_document_parsed_content(
    kb_id: str,
    doc_id: str,
    body: DocumentParsedContentUpdate,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.update_parsed_content(session, user_id, kb_id, doc_id, body)


@router.post(
    "/{kb_id}/documents/{doc_id}/confirm-import", response_model=DocumentConfirmImportResponse
)
async def confirm_document_import(
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.confirm_document_import(
        session, user_id, kb_id, doc_id, background_tasks
    )


@router.get("/{kb_id}/documents/{doc_id}/file")
async def get_document_file(
    kb_id: str,
    doc_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    doc = await document_service.get_document(session, user_id, kb_id, doc_id)
    path = document_service.document_filesystem_path(doc)
    return FileResponse(
        path,
        media_type=document_service.document_media_type(doc),
        filename=doc.filename,
        content_disposition_type="inline",
    )


@router.post("/{kb_id}/documents/{doc_id}/retry-parse", response_model=DocumentOut)
async def retry_parse_document(
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.retry_document_parse(
        session, user_id, kb_id, doc_id, background_tasks
    )


@router.post("/{kb_id}/documents/{doc_id}/reindex", response_model=DocumentOut)
async def reindex_document(
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.reindex_document(
        session, user_id, kb_id, doc_id, background_tasks
    )


@router.post(
    "/{kb_id}/documents/{doc_id}/versions",
    response_model=DocumentVersionUploadResponse,
)
async def upload_document_version(
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.upload_document_version(
        session, user_id, kb_id, doc_id, file, background_tasks
    )


@router.get(
    "/{kb_id}/documents/{doc_id}/versions",
    response_model=list[DocumentRevisionOut],
)
async def list_document_versions(
    kb_id: str,
    doc_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.list_document_versions(session, user_id, kb_id, doc_id)


@router.post(
    "/{kb_id}/documents/{doc_id}/versions/{revision_id}/rollback",
    response_model=DocumentVersionUploadResponse,
)
async def rollback_document_version(
    kb_id: str,
    doc_id: str,
    revision_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.rollback_document_version(
        session, user_id, kb_id, doc_id, revision_id, background_tasks
    )


@router.post(
    "/{kb_id}/documents/{doc_id}/versions/{revision_id}/retry",
    response_model=DocumentVersionUploadResponse,
)
async def retry_document_version(
    kb_id: str,
    doc_id: str,
    revision_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.retry_document_version(
        session, user_id, kb_id, doc_id, revision_id, background_tasks
    )


@router.post(
    "/{kb_id}/documents/{doc_id}/reconcile-indexes",
    response_model=DocumentIndexReconcileResponse,
)
async def reconcile_document_indexes(
    kb_id: str,
    doc_id: str,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return await document_service.reconcile_document_indexes(session, user_id, kb_id, doc_id)


@router.delete("/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    kb_id: str,
    doc_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    await document_service.delete_document(session, user_id, kb_id, doc_id, background_tasks)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
