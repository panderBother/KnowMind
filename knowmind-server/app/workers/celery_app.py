"""
Celery 应用定义：文档解析、向量化、批量评测等耗时任务放入队列执行。

- 默认（方案 B）：REDIS_URL + CELERY_TASK_ALWAYS_EAGER=false，并启动 Worker：
  `celery -A app.workers.celery_app.celery_app worker -l info`
- 无 Redis：`CELERY_TASK_ALWAYS_EAGER=true` 时使用内存 broker、关闭 result backend，
  `delay()` 在 API 进程内同步执行（无需 Worker）。
- Windows：默认 prefork 多进程与 billiard 易触发「句柄无效/拒绝访问」，故强制 `worker_pool=solo`
  （单进程顺序执行任务；开发够用）。Linux 上仍为 prefork，可用环境变量覆盖，见 `celery_worker_pool`。
"""

import sys

from celery import Celery

from app.core.config import get_settings


def _build_celery() -> Celery:
    s = get_settings()
    if s.celery_task_always_eager:
        # 避免未启动 Redis 时 send_task 仍连接 result backend 导致 500
        broker = "memory://"
        backend = None
    else:
        broker = s.redis_url
        backend = s.redis_url

    app = Celery(
        "knowmind",
        broker=broker,
        backend=backend,
    )
    app.conf.task_default_queue = "knowmind-default"
    app.conf.task_always_eager = s.celery_task_always_eager
    app.conf.task_eager_propagates = True
    if s.index_reconcile_interval_seconds > 0:
        app.conf.beat_schedule = {
            "reconcile-document-indexes": {
                "task": "documents.reconcile_indexes",
                "schedule": float(s.index_reconcile_interval_seconds),
            }
        }

    pool = s.celery_worker_pool
    if pool:
        app.conf.worker_pool = pool
    elif sys.platform == "win32":
        app.conf.worker_pool = "solo"

    return app


celery_app = _build_celery()

# 注册任务模块（依赖 celery_app 已实例化）
import app.workers.document_tasks  # noqa: E402, F401, I001
import app.workers.memory_tasks  # noqa: E402, F401, I001
