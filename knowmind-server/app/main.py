from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging_setup import configure_app_logging, log_info
from app.db.session import close_db, init_db
from app.middleware.request_observability import RequestObservabilityMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_app_logging()
    s = get_settings()
    log_info(
        "KnowMind 已启动 file_tools_enabled=%s file_tools_mode=%s",
        s.file_tools_enabled,
        s.file_tools_mode,
    )
    if s.database_url:
        await init_db(s.database_url)
    from app.services.eval_bootstrap import bootstrap_eval_report

    bootstrap_eval_report()
    yield
    s = get_settings()
    if s.database_url:
        await close_db()


# FastAPI 应用工厂：集中注册路由与 CORS，便于测试时复用 app 实例
_settings = get_settings()
app = FastAPI(title=_settings.project_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(RequestObservabilityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=_settings.api_v1_prefix)


@app.get("/")
def root():
    """根路径：部署探活或网关默认页。"""
    return {"service": get_settings().project_name, "docs": "/docs"}
