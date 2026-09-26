"""对话附件上传 API。"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import get_current_user_id
from app.core.config import settings
from app.services import chat_attachment_service as att_svc
from app.services.rate_limit_service import enforce_upload_limits

router = APIRouter()


@router.post("")
async def upload_chat_attachment(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    await enforce_upload_limits(user_id)
    max_bytes = settings.chat_attachment_max_mb * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if not data:
        raise HTTPException(400, "空文件")
    try:
        return await att_svc.save_attachment(user_id, file.filename or "upload.bin", data)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/{attachment_id}", response_class=FileResponse)
async def download_chat_attachment(
    attachment_id: str,
    user_id: str = Depends(get_current_user_id),
):
    try:
        path, filename = att_svc.resolve_attachment_for_user(user_id, attachment_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, filename=filename, media_type="application/octet-stream")
