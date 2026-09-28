from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])


class ChatRequest(BaseModel):
    """对话请求体：与前端 Chat 页对齐的最小字段集。"""

    message: str = Field(default="", max_length=8000)
    knowledge_base_id: str | None = None
    deep_research: bool = False
    web_search: bool = False
    model_mode: Literal["fast", "balanced", "deep"] = "balanced"
    arxiv: bool = Field(default=False, description="启用 arXiv 学术检索")
    semantic_scholar: bool = Field(default=False, description="启用 Semantic Scholar 检索")
    file_tools: bool = Field(
        default=False,
        description="启用本地文件读写工具（需服务端 file_tools_enabled）",
    )
    external_mcp: bool = Field(
        default=False,
        description="启用已配置的远程 URL 型外部 MCP 工具（需在工具页导入并开启）",
    )
    attachment_ids: list[str] = Field(
        default_factory=list,
        description="对话附件 id（先 POST /chat/attachments 上传）",
    )
    conversation_id: str | None = Field(
        default=None,
        description="不传或空则新建会话；传入已有 id 则续聊并写入 MySQL/Redis/向量索引",
    )

    @field_validator("conversation_id", mode="before")
    @classmethod
    def _empty_conv_id_none(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return str(v).strip()

    @model_validator(mode="after")
    def _message_or_attachments(self) -> "ChatRequest":
        if not (self.message or "").strip() and not self.attachment_ids:
            raise ValueError("请输入消息或上传至少一个附件")
        return self


class ChatResponse(BaseModel):
    reply: str
    trace_id: str
