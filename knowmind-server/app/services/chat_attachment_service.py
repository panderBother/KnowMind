"""对话附件：临时上传与上下文注入（含图片 OCR/视觉理解）。"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import settings
from app.ingest.ocr import ocr_image_bytes
from app.ingest.registry import detect_file_type, parse_file
from app.ingest.types import FileType
from app.ingest.vlm import analyze_image_for_chat_async

_CONTEXT_LIMIT = 16000


def _max_bytes() -> int:
    return settings.chat_attachment_max_mb * 1024 * 1024


def attachment_root() -> Path:
    p = Path(settings.chat_attachment_root)
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_dir(user_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user_id)
    d = attachment_root() / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


async def save_attachment(user_id: str, filename: str, data: bytes) -> dict:
    if len(data) > _max_bytes():
        raise ValueError(f"附件过大（上限 {settings.chat_attachment_max_mb}MB）")
    safe_filename = Path(filename).name
    ft = detect_file_type(safe_filename)
    if ft == FileType.UNKNOWN:
        raise ValueError("不支持的附件格式")
    att_id = str(uuid.uuid4())
    path = user_dir(user_id) / f"{att_id}_{safe_filename}"
    path.write_bytes(data)
    return {"id": att_id, "filename": safe_filename, "file_type": ft.value, "size": len(data)}


def _resolve_attachment_path(user_id: str, att_id: str) -> Path | None:
    att_id = (att_id or "").strip()
    if not att_id:
        return None
    try:
        normalized_id = str(uuid.UUID(att_id))
    except ValueError:
        return None
    matches = list(user_dir(user_id).glob(f"{normalized_id}_*"))
    return matches[0] if matches else None


def resolve_attachment_for_user(user_id: str, att_id: str) -> tuple[Path, str]:
    """解析当前用户的附件，绝不跨用户目录返回文件。"""
    path = _resolve_attachment_path(user_id, att_id)
    if path is None or not path.is_file():
        raise ValueError("附件不存在")
    filename = path.name.split("_", 1)[-1] if "_" in path.name else path.name
    return path, filename


def attachment_metadata(user_id: str, attachment_ids: list[str]) -> list[dict]:
    """生成可随聊天消息持久化的安全附件元数据。"""
    out: list[dict] = []
    for att_id in attachment_ids:
        path = _resolve_attachment_path(user_id, att_id)
        if path is None:
            continue
        name = path.name.split("_", 1)[-1] if "_" in path.name else path.name
        out.append(
            {
                "id": att_id,
                "filename": name,
                "file_type": detect_file_type(path.name).value,
                "size": path.stat().st_size,
            }
        )
    return out


async def _parse_attachment_content(path: Path, file_type: FileType) -> str:
    if file_type == FileType.IMAGE:
        content = await analyze_image_for_chat_async(str(path))
        if not content.strip():
            raw = path.read_bytes()
            local = ocr_image_bytes(raw)
            if local.strip():
                content = "## 文字内容（本地 OCR）\n\n" + local.strip()
            else:
                content = (
                    "（未能识图。请配置 SILICONFLOW_API_KEY 或 EDGEFN_API_KEY 以启用云端 OCR/视觉理解，"
                    "或安装 Tesseract 作为本地兜底。）"
                )
        return content.strip()

    result = parse_file(str(path), filename=path.name)
    return result.merged_content().strip()


async def load_attachment_context_async(user_id: str, attachment_ids: list[str]) -> str:
    if not attachment_ids:
        return ""
    parts: list[str] = []
    for att_id in attachment_ids:
        path = _resolve_attachment_path(user_id, att_id)
        if path is None:
            parts.append(f"（附件 {att_id} 不存在或已过期）")
            continue
        ft = detect_file_type(path.name)
        try:
            content = await _parse_attachment_content(path, ft)
            if len(content) > _CONTEXT_LIMIT:
                content = content[:_CONTEXT_LIMIT] + "\n…（已截断）"
            label = path.name.split("_", 1)[-1] if "_" in path.name else path.name
            parts.append(f"### 附件：{label}\n\n{content or '（无文本内容）'}")
        except Exception as e:
            parts.append(f"（附件 {path.name} 解析失败：{e!s}）")
    if not parts:
        return ""
    return "## 用户上传附件\n\n" + "\n\n".join(parts)


def load_attachment_context(user_id: str, attachment_ids: list[str]) -> str:
    """同步包装（深度研究等路径）。"""
    import asyncio  # noqa: PLC0415

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures  # noqa: PLC0415

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    lambda: asyncio.run(load_attachment_context_async(user_id, attachment_ids)),
                ).result()
        return loop.run_until_complete(load_attachment_context_async(user_id, attachment_ids))
    except RuntimeError:
        return asyncio.run(load_attachment_context_async(user_id, attachment_ids))
