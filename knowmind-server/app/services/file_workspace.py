"""Web 端文件读写：每个用户只能访问独立沙箱目录。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from app.core.config import settings

FILE_TOOLS_SYSTEM_HINT = (
    "用户已开启文件读写。所有路径都相对于该用户的私有工作区；"
    "禁止访问工作区之外的绝对路径。需要保存或读取文件时调用工具完成。"
)

Format = Literal["markdown", "text", "auto"]


def _safe_user_id(user_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(user_id))
    if not safe:
        raise ValueError("用户标识无效")
    return safe


def user_workspace(user_id: str) -> Path:
    root = Path(settings.user_workspace_root).resolve()
    target = (root / _safe_user_id(user_id)).resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve_user_path(user_id: str, raw_path: str) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise ValueError("path 不能为空")
    root = user_workspace(user_id)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("路径超出当前用户的私有工作区") from exc
    return candidate


def _normalize_extension(path: Path, fmt: Format) -> Path:
    if fmt == "auto" or path.suffix:
        return path
    return path.with_suffix(".md" if fmt == "markdown" else ".txt")


def list_allowed_roots_payload(user_id: str) -> dict:
    return {"allowed_roots": [str(user_workspace(user_id))], "hint_env": "用户私有工作区"}


def read_document(user_id: str, path: str, *, max_bytes: int | None = None) -> dict:
    target = resolve_user_path(user_id, path)
    if not target.is_file():
        raise ValueError(f"不是文件或不存在: {target}")
    limit = max_bytes if max_bytes is not None else settings.file_read_max_bytes
    with target.open("rb") as fp:
        raw = fp.read(limit + 1)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return {
        "path": str(target),
        "content": raw.decode("utf-8", errors="replace"),
        "truncated": truncated,
        "size_bytes": target.stat().st_size,
        "status": "read",
    }


def write_document(
    user_id: str,
    path: str,
    content: str,
    *,
    format: Format = "auto",
    overwrite: bool = True,
) -> dict:
    target = _normalize_extension(resolve_user_path(user_id, path), format)
    if target.exists() and not overwrite:
        raise ValueError(f"文件已存在: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": str(target),
        "bytes_written": len(content.encode("utf-8")),
        "format": "utf-8 text",
        "status": "written",
    }


def execute_tool(
    user_id: str,
    name: str,
    arguments_json: str,
    *,
    max_read_bytes: int | None = None,
) -> str:
    try:
        args = json.loads(arguments_json) if arguments_json.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("参数必须是 JSON 对象")
        if name == "read_document":
            result = read_document(
                user_id,
                str(args.get("path", "")),
                max_bytes=max_read_bytes,
            )
        elif name in ("write_document", "write_markdown"):
            raw_path = args.get("filename", args.get("path", ""))
            fmt = "markdown" if name == "write_markdown" else args.get("format", "auto")
            result = write_document(
                user_id,
                str(raw_path),
                str(args.get("content", "")),
                format=fmt,
                overwrite=bool(args.get("overwrite", True)),
            )
        elif name == "list_allowed_write_roots":
            result = list_allowed_roots_payload(user_id)
        else:
            result = {"error": f"未知工具: {name}"}
    except Exception as exc:  # noqa: BLE001
        result = {"error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


def max_read_bytes() -> int:
    return settings.file_read_max_bytes
