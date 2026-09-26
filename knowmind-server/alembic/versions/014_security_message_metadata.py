"""token revocation and durable chat message metadata

Revision ID: 014_security_message_metadata
Revises: 013_planner_memory_incremental
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_security_message_metadata"
down_revision: Union[str, None] = "013_planner_memory_incremental"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("chat_messages", sa.Column("citations_json", sa.JSON(), nullable=True))
    op.add_column("chat_messages", sa.Column("attachments_json", sa.JSON(), nullable=True))
    op.add_column("chat_messages", sa.Column("tool_traces_json", sa.JSON(), nullable=True))
    op.add_column(
        "chat_messages",
        sa.Column("reply_to_message_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_chat_messages_reply_to_message_id",
        "chat_messages",
        ["reply_to_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_reply_to_message_id", table_name="chat_messages")
    op.drop_column("chat_messages", "reply_to_message_id")
    op.drop_column("chat_messages", "tool_traces_json")
    op.drop_column("chat_messages", "attachments_json")
    op.drop_column("chat_messages", "citations_json")
    op.drop_column("users", "token_version")
