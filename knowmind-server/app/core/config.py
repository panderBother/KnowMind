from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# knowmind-server 根目录（与 shell cwd 无关，避免 API / Worker 各读各的 .env 与 data/storage）
_SERVER_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """
    运行时配置：优先读取环境变量，便于 Docker / K8s 注入。
    本地开发可复制 `knowmind-server/env.example` 为 `.env`。
    """

    model_config = SettingsConfigDict(
        env_file=str(_SERVER_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    project_name: str = "KnowMind API"
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # MySQL 异步 DSN，例：mysql+asyncmy://user:pass@127.0.0.1:3306/knowmind
    database_url: str | None = None

    jwt_secret: str = Field(
        default="dev-only-change-me",
        description="生产环境必须通过环境变量覆盖",
    )
    jwt_algorithm: str = "HS256"
    access_token_expire_days: int = 7

    # 本地文件存储根目录（后续可换 OSS 适配器）
    storage_local_root: str = "data/storage"

    redis_url: str = "redis://localhost:6379/0"
    agent_service_url: str = "http://127.0.0.1:9100"

    max_knowledge_bases_per_user: int = 10

    # Skill/MCP 导出：对外展示的 API 基址（如 https://your-host/api/v1）；未设则从请求推断
    public_api_base_url: str | None = None
    knowmind_mcp_root: str | None = None

    # RAG 评估看板：knowmind-eval/reports 目录；未设则相对仓库根目录自动解析
    eval_reports_dir: str | None = None

    # 向量：Windows 无 Milvus Lite wheel；默认 Chroma 持久化。远程 Milvus 示例：http://127.0.0.1:19530
    milvus_uri: str | None = None
    chroma_data_path: str = "data/chroma"
    whoosh_index_root: str = "data/whoosh"

    # 向量嵌入：`bge` 本地 sentence-transformers；`http` 调用 OpenAI 兼容 POST …/embeddings（白山智算 / EdgeFN 等）；`hash` 用于 CI
    embedding_mode: str = Field(default="bge", description="bge | hash | http")
    embedding_model_id: str = "BAAI/bge-m3"
    embedding_vector_dim: int = 1024
    embedding_batch_size: int = 24

    # embedding_mode=http：默认 base/key 与 EdgeFN 对话共用（也可用白山智算等平台分配的网关）
    embedding_http_base_url: str | None = None
    embedding_http_api_key: str | None = None
    embedding_http_model: str = "BAAI/bge-m3"

    chroma_collection_name: str = "doc_chunks_bge_m3"
    # 对话记忆向量：与文档 doc_chunks 分 collection，避免 metadata 混用
    chroma_chat_collection_name: str = "chat_memory_bge_m3"
    milvus_collection_name: str = "scholar_doc_chunks_bge_m3"

    # 对话记忆（可选覆盖 memory_constants 默认值）
    memory_recent_message_count: int = 8
    memory_recent_max_tokens: int = 4000
    memory_summary_trigger_turns: int = 10
    memory_summary_trigger_tokens: int = 8000
    memory_summary_cooldown_turns: int = 6
    memory_summary_cooldown_tokens: int = 4000
    memory_retrieval_max_tokens: int = 2000
    memory_retrieval_top_k: int = 10

    # RAG：精排后注入对话/展示的片段条数
    rag_top_k: int = 8
    # 混合检索各路召回（RRF 融合 → Rerank → 取 rag_top_k）
    rag_vector_top_k: int = 15
    rag_bm25_top_k: int = 20
    # 兼容旧配置；未单独设 vector/bm25 时的兜底候选池
    rag_candidate_k: int = 32
    # 入库语义切块：过短合并下限、单块上限（字符）
    chunk_min_chars: int = 120
    chunk_max_chars: int = 640
    chunk_overlap: int = 100
    # 管理端搜索：向量/Rerank 语义分低于此阈值视为未命中
    rag_min_relevance_score: float = 0.45
    # 对话 RAG 更严：未达阈值则不注入上下文、不展示引用来源
    rag_chat_min_relevance_score: float = 0.5

    # BGE-Reranker 精排（P1）；hash 嵌入模式下自动跳过
    rerank_enabled: bool = True
    # 对话 RAG 走 Rerank 精排后再决定是否引用；管理端搜索页同样可用 hybrid_search
    rag_chat_skip_rerank: bool = False
    # `http` 调用白山智算 / EdgeFN POST …/v1/rerank；`local` 本机 CrossEncoder（需下载权重）
    rerank_mode: str = Field(default="http", description="http | local")
    rerank_model_id: str = "BAAI/bge-reranker-v2-m3"
    # rerank_mode=http：默认 base/key 与 EdgeFN / 嵌入共用
    rerank_http_base_url: str | None = None
    rerank_http_api_key: str | None = None
    rerank_http_model: str = "bge-reranker-v2-m3"
    rerank_min_score: float | None = None

    # 对外 HTTP：false=不走系统 HTTP_PROXY（Windows 代理下易出现 SSL UNEXPECTED_EOF）
    http_trust_env: bool = False
    http_max_retries: int = 3

    # 知识蒸馏 Demo 模式（降低触发阈值，便于答辩演示）
    distill_demo_mode: bool = True

    # true：上传后在 API 进程内起 daemon 线程解析（无需 Redis/Celery Worker，适合本机开发）；生产请 false 并用 Worker
    ingest_background_thread: bool = False
    index_reconcile_interval_seconds: int = 1800
    ingest_processing_timeout_seconds: int = 3600

    # 同一进程内同时解析的文档数（Windows + HF 下载模型建议为 1，避免缓存损坏 / 进度卡 0%）
    ingest_max_parallel: int = 1

    pdf_max_upload_mb: int = 50
    pdf_max_batch: int = 20

    # EdgeFN OpenAI 兼容对话：https://api.edgefn.net/v1/chat/completions
    edgefn_api_key: str | None = Field(default=None, description="Bearer Token，勿提交仓库")
    edgefn_api_base_url: str = "https://api.edgefn.net/v1"
    edgefn_chat_model: str = "DeepSeek-R1-0528-Qwen3-8B"
    # 识图专用；留空则不对 EdgeFN 发 image_url（避免纯文本对话模型 400）
    edgefn_vision_model: str | None = Field(
        default=None, description="支持 vision 的 EdgeFN 模型名，勿与 edgefn_chat_model 混用"
    )

    # 硅基流动：文档/图片 OCR（DeepSeek-OCR），与 EdgeFN 对话独立
    siliconflow_api_key: str | None = Field(default=None, description="Bearer Token，勿提交仓库")
    siliconflow_api_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_vision_model: str = "deepseek-ai/DeepSeek-OCR"

    # 对话中本地文件读写；白名单见 FILE_WRITER_ALLOWED_ROOTS
    file_tools_enabled: bool = True
    # prompt：模型输出 XML 工具块（兼容 EdgeFN / 未开 native tools 的网关）；native：OpenAI tools API
    file_tools_mode: str = Field(default="prompt", description="prompt | native")
    file_tools_max_rounds: int = 6
    file_read_max_bytes: int = 512 * 1024

    # 联网搜索（Brave 优先；无 Key 时 DuckDuckGo 即时答案）
    web_search_enabled: bool = True
    brave_search_api_key: str | None = None
    web_search_max_results: int = 5

    # 学术检索 MCP
    arxiv_search_enabled: bool = True
    arxiv_max_results: int = 5
    semantic_scholar_enabled: bool = True
    semantic_scholar_api_key: str | None = None
    semantic_scholar_max_results: int = 5

    # JWT 刷新令牌（天）
    refresh_token_expire_days: int = 30

    # 对话附件临时目录（相对 knowmind-server 根）
    chat_attachment_root: str = "data/chat_attachments"
    chat_attachment_max_mb: int = 10

    # 评估：启动时若 reports/latest.json 缺失则尝试生成 sample
    eval_auto_bootstrap: bool = True
    external_mcp_enabled: bool = True
    # prompt：不传 EdgeFN tools（兼容未开 vLLM auto tool choice）；native：OpenAI tools API
    external_mcp_tool_mode: str = Field(default="prompt", description="prompt | native")
    external_mcp_max_rounds: int = 4
    external_mcp_connect_timeout: float = 15.0
    external_mcp_read_timeout: float = 120.0

    # false（方案 B）：Redis + Celery Worker 异步解析。true 时仅用内存 broker、无 Redis 也可开发。
    celery_task_always_eager: bool = False
    # 留空则 Windows 自动 solo；Linux 默认 prefork。可显式设为 prefork|solo|threads|gevent
    celery_worker_pool: str | None = None

    @model_validator(mode="after")
    def _resolve_relative_paths(self) -> Self:
        """相对路径一律相对 knowmind-server 根目录，避免从仓库根启动时与 Worker 不一致。"""
        for name in (
            "storage_local_root",
            "chroma_data_path",
            "whoosh_index_root",
            "chat_attachment_root",
        ):
            raw = getattr(self, name)
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                setattr(self, name, str((_SERVER_ROOT / p).resolve()))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
