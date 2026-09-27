from fastapi import APIRouter, Depends

from app.api.deps import get_current_user_id
from app.db.session import get_session_factory
from app.models.schemas import ChatRequest, ChatResponse
from app.services.chat_sync import collect_chat_response
from app.services.chat_service import iter_chat_stream
from app.services.rate_limit_service import enforce_chat_limits
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.post("", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user_id: str = Depends(get_current_user_id),
):
    """同步对话：内部复用 `/chat/stream` 同一套 RAG / 记忆 / 工具 / 深度研究逻辑。"""
    await enforce_chat_limits(user_id)
    factory = get_session_factory()
    async with factory() as session:
        return await collect_chat_response(req, session=session, user_id=user_id)


@router.post("/stream")
async def chat_stream(
    req: ChatRequest,
    user_id: str = Depends(get_current_user_id),
):
    """SSE 流式对话：先下发 trace_id，再在连接内完成 RAG / 记忆组装与模型流式输出。"""

    await enforce_chat_limits(user_id)
    factory = get_session_factory()

    async def gen():
        async with factory() as session:
            async for line in iter_chat_stream(
                req,
                session=session,
                user_id=user_id,
            ):
                yield line

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
