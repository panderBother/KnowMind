"""启动时确保评估看板有 sample 报告。"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from app.core.config import get_settings

log = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def bootstrap_eval_report() -> None:
    settings = get_settings()
    if not settings.eval_auto_bootstrap:
        return
    reports_dir = Path(settings.eval_reports_dir) if settings.eval_reports_dir else _repo_root() / "knowmind-eval" / "reports"
    latest = reports_dir / "latest.json"
    if latest.is_file():
        return
    pipeline = _repo_root() / "knowmind-eval" / "pipelines" / "run_eval.py"
    if not pipeline.is_file():
        log.warning("eval bootstrap skipped: pipeline not found")
        return
    try:
        subprocess.run(
            [
                sys.executable,
                str(pipeline),
                "--dataset",
                "sample.jsonl",
                "--mode",
                "offline",
            ],
            check=True,
            cwd=str(pipeline.parent.parent),
            capture_output=True,
            text=True,
            timeout=120,
        )
        log.info("eval bootstrap wrote %s", latest)
    except Exception as e:
        log.warning("eval bootstrap failed: %s", e)
