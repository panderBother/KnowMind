"""conversation branches, pinning, model mode and generation state

Revision ID: 016_conversation_experience
Revises: 015_document_versioning
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016_conversation_experience"
down_revision: Union[str, None] = "015_document_versioning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("parent_conversation_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("branched_from_message_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "conversations",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "conversations",
        sa.Column("model_mode", sa.String(length=20), nullable=False, server_default="balanced"),
    )
    op.create_index(
        "ix_conversations_parent_conversation_id", "conversations", ["parent_conversation_id"]
    )
    op.create_index("ix_conversations_is_pinned", "conversations", ["is_pinned"])
    op.add_column(
        "chat_messages",
        sa.Column("sequence_no", sa.Integer(), nullable=False, server_default="0"),
    )
    chat_messages = sa.table(
        "chat_messages",
        sa.column("id", sa.String(length=36)),
        sa.column("conversation_id", sa.String(length=36)),
        sa.column("sequence_no", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.select(chat_messages.c.id, chat_messages.c.conversation_id).order_by(
                chat_messages.c.conversation_id,
                chat_messages.c.created_at,
                chat_messages.c.id,
            )
        )
    )
    counters: dict[str, int] = {}
    updates: list[dict[str, object]] = []
    for message_id, conversation_id in rows:
        sequence_no = counters.get(conversation_id, 0) + 1
        counters[conversation_id] = sequence_no
        updates.append({"message_id": message_id, "new_sequence_no": sequence_no})
    if updates:
        connection.execute(
            chat_messages.update()
            .where(chat_messages.c.id == sa.bindparam("message_id"))
            .values(sequence_no=sa.bindparam("new_sequence_no")),
            updates,
        )
    op.create_index(
        "ix_chat_messages_conversation_sequence",
        "chat_messages",
        ["conversation_id", "sequence_no"],
    )
    op.add_column(
        "chat_messages",
        sa.Column(
            "generation_status",
            sa.String(length=20),
            nullable=False,
            server_default="completed",
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "generation_status")
    op.drop_index("ix_chat_messages_conversation_sequence", table_name="chat_messages")
    op.drop_column("chat_messages", "sequence_no")
    op.drop_index("ix_conversations_is_pinned", table_name="conversations")
    op.drop_index("ix_conversations_parent_conversation_id", table_name="conversations")
    op.drop_column("conversations", "model_mode")
    op.drop_column("conversations", "is_pinned")
    op.drop_column("conversations", "branched_from_message_id")
    op.drop_column("conversations", "parent_conversation_id")
