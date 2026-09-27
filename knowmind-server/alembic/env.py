from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# 保证 `import app` 以 knowmind-server 为根
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models.orm import (  # noqa: F401, E402
    ChatMessage,
    Conversation,
    ConversationSummary,
    ConversationFact,
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    User,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_mysql_url() -> str:
    settings = get_settings()
    url = settings.database_url
    if not url:
        raise RuntimeError("请设置环境变量 DATABASE_URL（mysql+asyncmy://...）后执行迁移")
    if "+asyncmy" not in url:
        return url
    return url.replace("+asyncmy", "+pymysql", 1)


def run_migrations_offline() -> None:
    url = _sync_mysql_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sync_mysql_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
