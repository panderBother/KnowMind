from fastapi import APIRouter

from app.api.v1.endpoints import (
    analytics,
    auth,
    chat,
    chat_attachments,
    chat_feedback,
    conversations,
    distill,
    documents,
    evaluation,
    experts,
    health,
    knowledge_bases,
    knowledge_categories,
    knowledge_items,
    mcp_tools,
    observability,
    reports,
    workspace_files,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(knowledge_bases.router, prefix="/knowledge-bases", tags=["knowledge-bases"])
api_router.include_router(analytics.router, prefix="/knowledge-bases", tags=["analytics"])
api_router.include_router(knowledge_items.router, prefix="/knowledge-bases", tags=["knowledge-items"])
api_router.include_router(knowledge_categories.router, prefix="/knowledge-bases", tags=["knowledge-categories"])
api_router.include_router(documents.router, prefix="/knowledge-bases", tags=["documents"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
api_router.include_router(reports.router, prefix="/reports", tags=["reports"])
api_router.include_router(experts.router, prefix="/experts", tags=["experts"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(chat_attachments.router, prefix="/chat/attachments", tags=["chat"])
api_router.include_router(chat_feedback.router, prefix="/chat", tags=["chat"])
api_router.include_router(distill.router, prefix="/knowledge-bases", tags=["distill"])
api_router.include_router(
    workspace_files.router,
    prefix="/workspace/files",
    tags=["workspace-files"],
)
api_router.include_router(mcp_tools.router, prefix="/mcp/tools", tags=["mcp-tools"])
api_router.include_router(evaluation.router, prefix="/evaluation", tags=["evaluation"])
api_router.include_router(observability.router, prefix="/observability", tags=["observability"])
