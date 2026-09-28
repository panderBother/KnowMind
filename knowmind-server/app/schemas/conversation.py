from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ConversationCreate(BaseModel):
    knowledge_base_id: str | None = None
    deep_research: bool = False
    web_search: bool = False
    title: str | None = Field(default=None, max_length=255)
    model_mode: Literal["fast", "balanced", "deep"] = "balanced"


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    is_pinned: bool | None = None


class ConversationBranchRequest(BaseModel):
    from_message_id: str = Field(min_length=1, max_length=36)
    title: str | None = Field(default=None, max_length=255)


class ConversationOut(BaseModel):
    id: str
    knowledge_base_id: str | None
    expert_id: str | None = None
    parent_conversation_id: str | None = None
    branched_from_message_id: str | None = None
    is_pinned: bool = False
    model_mode: Literal["fast", "balanced", "deep"] = "balanced"
    deep_research: bool
    web_search: bool
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatMessageOut(BaseModel):
    id: str
    role: str
    content: str
    trace_id: str | None
    generation_status: str = "completed"
    citations: list[dict] | None = Field(default=None, validation_alias="citations_json")
    attachments: list[dict] | None = Field(default=None, validation_alias="attachments_json")
    tool_traces: list[dict] | None = Field(default=None, validation_alias="tool_traces_json")
    created_at: datetime

    @field_validator("generation_status", mode="before")
    @classmethod
    def _default_generation_status(cls, value: object) -> str:
        return str(value) if value else "completed"

    model_config = {"from_attributes": True}
