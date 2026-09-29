"""
测试启动前移除外部注入的 DATABASE_URL，避免本机 .env 中的 MySQL 在 pytest 进程内被误连。
业务测试通过依赖覆盖或独立 sqlite 文件自行初始化 schema。
"""

from __future__ import annotations

import os

os.environ.pop("DATABASE_URL", None)
# pytest 默认不拉取 BGE-M3，避免下载大模型；Worker 集成测试可显式改为 bge
os.environ.setdefault("EMBEDDING_MODE", "hash")
os.environ["INGEST_BACKGROUND_THREAD"] = "false"
