"""读取 knowmind-eval 流水线产出的 JSON 报告，供评估看板 API 使用。"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import get_settings
from app.schemas.evaluation import (
    EvalDashboardOut,
    EvalKpiOut,
    EvalStatsOut,
    EvalTrendPointOut,
    EvalVersionCompareOut,
)

_METRIC_KEYS = (
    "faithfulness",
    "answer_relevancy",
    "context_recall",
    "context_precision",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _reports_dir() -> Path:
    settings = get_settings()
    if settings.eval_reports_dir:
        return Path(settings.eval_reports_dir)
    return _repo_root() / "knowmind-eval" / "reports"


def _empty_dashboard(*, note: str | None = None) -> EvalDashboardOut:
    kpis = {
        key: EvalKpiOut(value=0.0, delta=0.0)
        for key in _METRIC_KEYS
    }
    stats = EvalStatsOut(
        total_runs=0,
        question_count=0,
        avg_latency_s=0.0,
        p95_latency_s=0.0,
        avg_ttft_s=None,
        pass_rate=0.0,
        failed_cases=0,
    )
    out = EvalDashboardOut(mode="stub", kpis=kpis, stats=stats)
    if note:
        out.run_id = note
    return out


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_dashboard() -> EvalDashboardOut:
    latest_path = _reports_dir() / "latest.json"
    if not latest_path.is_file():
        return _empty_dashboard(note="no_report")

    raw = _read_json(latest_path)
    if not raw or raw.get("status") == "stub":
        return _empty_dashboard(note="stub_report")

    metrics: dict = raw.get("metrics") or {}
    deltas: dict = raw.get("deltas") or {}
    kpis = {
        key: EvalKpiOut(
            value=float(metrics.get(key, 0.0)),
            delta=float(deltas.get(key, 0.0)),
        )
        for key in _METRIC_KEYS
    }

    trend = [
        EvalTrendPointOut.model_validate(row)
        for row in (raw.get("trend") or [])
    ]
    version_compare = [
        EvalVersionCompareOut.model_validate(row)
        for row in (raw.get("version_compare") or [])
    ]
    stats_raw = raw.get("stats") or {}
    stats = EvalStatsOut(
        total_runs=int(stats_raw.get("total_runs", 0)),
        question_count=int(stats_raw.get("question_count", raw.get("sample_count", 0))),
        avg_latency_s=float(stats_raw.get("avg_latency_s", 0.0)),
        p95_latency_s=float(stats_raw.get("p95_latency_s", 0.0)),
        avg_ttft_s=(
            float(stats_raw["avg_ttft_s"])
            if stats_raw.get("avg_ttft_s") is not None
            else None
        ),
        pass_rate=float(stats_raw.get("pass_rate", 0.0)),
        failed_cases=int(stats_raw.get("failed_cases", 0)),
    )

    return EvalDashboardOut(
        run_id=raw.get("run_id"),
        created_at=raw.get("created_at"),
        version=raw.get("version"),
        mode=str(raw.get("mode") or "simple"),
        sample_count=int(raw.get("sample_count", 0)),
        kpis=kpis,
        trend=trend,
        version_compare=version_compare,
        stats=stats,
    )


def run_sample_eval() -> EvalDashboardOut:
    import subprocess
    import sys

    root = _repo_root()
    pipeline = root / "knowmind-eval" / "pipelines" / "run_eval.py"
    if not pipeline.is_file():
        return _empty_dashboard(note="pipeline_missing")
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
            timeout=300,
        )
    except Exception:
        return _empty_dashboard(note="evaluation_failed")
    return get_dashboard()
