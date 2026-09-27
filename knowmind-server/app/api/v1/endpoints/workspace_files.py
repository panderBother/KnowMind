from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_current_user_id
from app.core.config import settings
from app.models.workspace_schemas import (
    AllowedRootsResponse,
    FileOpResponse,
    FileReadRequest,
    FileWriteRequest,
)
from app.services import file_workspace

router = APIRouter()


def _ensure_file_workspace() -> None:
    if not settings.file_tools_enabled:
        raise HTTPException(status_code=403, detail="服务端未启用文件工作区")


@router.get("/roots", response_model=AllowedRootsResponse)
async def list_roots(user_id: str = Depends(get_current_user_id)) -> AllowedRootsResponse:
    _ensure_file_workspace()
    data = file_workspace.list_allowed_roots_payload(user_id)
    return AllowedRootsResponse(**data)


@router.post("/read", response_model=FileOpResponse)
async def read_file(
    body: FileReadRequest,
    user_id: str = Depends(get_current_user_id),
) -> FileOpResponse:
    _ensure_file_workspace()
    try:
        data = file_workspace.read_document(
            user_id, body.path, max_bytes=file_workspace.max_read_bytes()
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return FileOpResponse(
        path=data.get("path"),
        content=data.get("content"),
        status=str(data.get("status", "read")),
        truncated=bool(data.get("truncated")),
        size_bytes=data.get("size_bytes"),
    )


@router.post("/write", response_model=FileOpResponse)
async def write_file(
    body: FileWriteRequest,
    user_id: str = Depends(get_current_user_id),
) -> FileOpResponse:
    _ensure_file_workspace()
    try:
        data = file_workspace.write_document(
            user_id,
            body.path,
            body.content,
            format=body.format,  # type: ignore[arg-type]
            overwrite=body.overwrite,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return FileOpResponse(
        path=data.get("path"),
        status=str(data.get("status", "written")),
        bytes_written=data.get("bytes_written"),
    )
