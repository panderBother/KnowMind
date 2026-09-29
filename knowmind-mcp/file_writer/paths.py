"""受控路径解析：仅允许写入配置过的根目录，防止路径穿越。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ENV_ALLOWED_ROOTS = "FILE_WRITER_ALLOWED_ROOTS"


def _default_allowed_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    roots.append(home.resolve())

    repo_root = Path(__file__).resolve().parents[2]
    exports = (repo_root / "data" / "exports").resolve()
    exports.mkdir(parents=True, exist_ok=True)
    roots.append(exports)

    if sys.platform == "win32":
        for letter in "DEFG":
            drive = Path(f"{letter}:/")
            if drive.exists():
                roots.append(drive.resolve())
    else:
        tmp = (Path(tempfile.gettempdir()) / "knowmind").resolve()
        tmp.mkdir(parents=True, exist_ok=True)
        roots.append(tmp)

    return roots


def allowed_roots() -> list[Path]:
    raw = os.environ.get(_ENV_ALLOWED_ROOTS, "").strip()
    if not raw:
        return _default_allowed_roots()
    roots: list[Path] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        roots.append(Path(part).expanduser().resolve())
    return roots or _default_allowed_roots()


def resolve_writable_path(path: str) -> Path:
    """
    将用户给出的路径解析为绝对路径，并校验落在允许根目录内。

    支持 Windows（如 D:\\reports\\note.md）与 POSIX 路径。
    """
    if not path or not str(path).strip():
        raise ValueError("path 不能为空")

    candidate = Path(path.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()

    roots = allowed_roots()
    for root in roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue

    allowed = ", ".join(str(r) for r in roots)
    raise ValueError(
        f"路径不在允许范围内: {candidate}。"
        f"可通过环境变量 {_ENV_ALLOWED_ROOTS} 配置允许根目录（分号分隔）。"
        f"当前允许: {allowed}"
    )
