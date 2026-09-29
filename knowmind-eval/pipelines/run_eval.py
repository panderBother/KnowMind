"""KnowMind RAG 评测：离线指标回归或真实 `/chat/stream` 端到端调用。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT / "datasets"
REPORTS_DIR = ROOT / "reports"
METRIC_KEYS = (
    "faithfulness",
    "answer_relevancy",
    "context_recall",
    "context_precision",
)


def _tokenize(text: str) -> set[str]:
    r"""同时覆盖英文词和中文单字，避免 `\w+` 把整句中文当成一个 Token。"""
    normalized = (text or "").lower()
    return set(re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", normalized))


def _jaccard(a: str, b: str) -> float:
    left, right = _tokenize(a), _tokenize(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        if not str(row.get("question") or "").strip():
            raise ValueError(f"line {line_number}: question is required")
        if not str(row.get("ground_truth") or "").strip():
            raise ValueError(f"line {line_number}: ground_truth is required")
        rows.append(row)
    return rows


def simple_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, list[float]] = {key: [] for key in METRIC_KEYS}
    for row in rows:
        answer = str(row.get("answer") or "")
        if not answer and not row.get("error"):
            raise ValueError(
                "answer is required for scoring; live mode must receive model output"
            )
        if row.get("error"):
            for scores in values.values():
                scores.append(0.0)
            continue
        ground_truth = str(row.get("ground_truth") or "")
        question = str(row.get("question") or "")
        context = " ".join(str(item) for item in (row.get("contexts") or []))
        values["faithfulness"].append(_jaccard(answer, context))
        values["answer_relevancy"].append(_jaccard(answer, question))
        values["context_recall"].append(_jaccard(context, ground_truth))
        values["context_precision"].append(_jaccard(context, answer))
    return {key: round(statistics.fmean(scores), 4) for key, scores in values.items()}


def ragas_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )

    result = evaluate(
        Dataset.from_list(rows),
        metrics=[faithfulness, answer_relevancy, context_recall, context_precision],
    )
    # RAGAS returns a per-case list for each metric, not a scalar dict.
    return {key: round(statistics.fmean(result[key]), 4) for key in METRIC_KEYS}


def _source_text(source: dict[str, Any]) -> str:
    for key in ("excerpt", "content", "text", "snippet", "title"):
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("data:"):
        return None
    try:
        payload = json.loads(line[5:].strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _login(client: httpx.Client, email: str, password: str) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    token = str(response.json().get("access_token") or "")
    if not token:
        raise RuntimeError("login response did not contain access_token")
    return token


def run_live_cases(
    rows: list[dict[str, Any]],
    *,
    api_base_url: str,
    token: str | None,
    email: str | None,
    password: str | None,
    knowledge_base_id: str | None,
    timeout_s: float,
) -> list[dict[str, Any]]:
    base_url = api_base_url.rstrip("/")
    if not base_url.endswith("/api/v1"):
        base_url = f"{base_url}/api/v1"
    generated: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url, timeout=timeout_s) as client:
        access_token = token
        if not access_token:
            if not email or not password:
                raise ValueError("live mode requires token or email/password")
            access_token = _login(client, email, password)
        headers = {"Authorization": f"Bearer {access_token}"}

        for index, row in enumerate(rows, 1):
            started = time.perf_counter()
            first_token_at: float | None = None
            answer_parts: list[str] = []
            contexts: list[str] = []
            trace_id: str | None = None
            error: str | None = None
            received_done = False
            sources: list[dict[str, Any]] = []
            body = {
                "message": row["question"],
                "knowledge_base_id": row.get("knowledge_base_id") or knowledge_base_id,
                "model_mode": row.get("model_mode") or "balanced",
                "web_search": bool(row.get("web_search", False)),
            }
            try:
                with client.stream(
                    "POST", "/chat/stream", json=body, headers=headers
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        event = _parse_sse_line(line)
                        if not event:
                            continue
                        event_type = event.get("type")
                        if event_type == "trace_id":
                            trace_id = str(event.get("trace_id") or "") or None
                        elif event_type == "delta":
                            if first_token_at is None:
                                first_token_at = time.perf_counter()
                            answer_parts.append(str(event.get("text") or ""))
                        elif event_type == "rag_sources":
                            sources.extend(
                                source for source in (event.get("sources") or [])
                                if isinstance(source, dict)
                            )
                            contexts.extend(
                                text
                                for text in (
                                    _source_text(source)
                                    for source in (event.get("sources") or [])
                                    if isinstance(source, dict)
                                )
                                if text
                            )
                        elif event_type == "error":
                            error = str(event.get("message") or "model stream error")
                        elif event_type == "done":
                            received_done = True
                            break
            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                error = str(exc)

            finished = time.perf_counter()
            answer = "".join(answer_parts).strip()
            generated.append(
                {
                    **row,
                    "answer": answer,
                    "contexts": contexts,
                    "sources": sources,
                    "case_id": str(row.get("id") or index),
                    "trace_id": trace_id,
                    "latency_s": round(finished - started, 4),
                    "ttft_s": (
                        round(first_token_at - started, 4)
                        if first_token_at is not None
                        else None
                    ),
                    "error": error or (
                        "incomplete stream: missing done" if not received_done
                        else None if answer else "empty model answer"
                    ),
                }
            )
    return generated


def _load_history_metrics() -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if not REPORTS_DIR.is_dir():
        return history
    for path in sorted(REPORTS_DIR.glob("*.json")):
        if path.name == "latest.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data.get("metrics"), dict):
            history.append(data)
    return sorted(history, key=lambda item: item.get("created_at") or "")


def _trend_point(item: dict[str, Any]) -> dict[str, Any]:
    metrics = item.get("metrics") or {}
    created = str(item.get("created_at") or "")
    return {
        "label": created[5:10].replace("-", "/") if len(created) >= 10 else "当前",
        **{key: round(float(metrics.get(key, 0)) * 100, 1) for key in METRIC_KEYS},
    }


def _build_version_compare(
    history: list[dict[str, Any]], metrics: dict[str, float]
) -> list[dict[str, Any]]:
    baseline = (history[-1].get("metrics") if history else None) or metrics
    labels = {
        "faithfulness": "忠实度",
        "answer_relevancy": "答案相关性",
        "context_recall": "上下文召回",
        "context_precision": "上下文精准",
    }
    return [
        {
            "name": name,
            "current": round(float(metrics.get(key, 0)) * 100, 1),
            "baseline": round(float(baseline.get(key, 0)) * 100, 1),
        }
        for key, name in labels.items()
    ]


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0.0


def run_eval(
    *,
    dataset: str,
    use_ragas: bool,
    version: str,
    mode: str = "offline",
    api_base_url: str = "http://127.0.0.1:8000",
    token: str | None = None,
    email: str | None = None,
    password: str | None = None,
    knowledge_base_id: str | None = None,
    timeout_s: float = 180,
    case_pass_threshold: float = 0.25,
    fail_under: float = 0.0,
) -> dict[str, Any]:
    if mode not in {"offline", "live"}:
        raise ValueError("mode must be offline or live")
    if not (0 <= case_pass_threshold <= 1 and 0 <= fail_under <= 1):
        raise ValueError("quality thresholds must be between 0 and 1")
    dataset_path = DATASETS_DIR / dataset
    if not dataset_path.is_file():
        raise FileNotFoundError(f"dataset not found: {dataset_path}")
    rows = load_jsonl(dataset_path)
    if not rows:
        raise ValueError(f"dataset empty: {dataset_path}")
    if mode == "live":
        rows = run_live_cases(
            rows,
            api_base_url=api_base_url,
            token=token,
            email=email,
            password=password,
            knowledge_base_id=knowledge_base_id,
            timeout_s=timeout_s,
        )
    else:
        missing = [index for index, row in enumerate(rows, 1) if not row.get("answer")]
        if missing:
            raise ValueError(f"offline dataset is missing answer at rows: {missing}")
        rows = [
            {
                **row,
                "case_id": str(row.get("id") or index),
                "latency_s": 0.0,
                "ttft_s": None,
            }
            for index, row in enumerate(rows, 1)
        ]

    metric_mode = "ragas" if use_ragas else "simple"
    metrics = simple_metrics(rows)
    if use_ragas:
        successful = [row for row in rows if not row.get("error")]
        judged = ragas_metrics(successful) if successful else dict.fromkeys(METRIC_KEYS, 0.0)
        metrics = {key: value * len(successful) / len(rows) for key, value in judged.items()}
    case_results: list[dict[str, Any]] = []
    for row in rows:
        answer_score = _jaccard(str(row.get("answer") or ""), str(row["ground_truth"]))
        citation_ok = not row.get("require_citations") or bool(row.get("contexts"))
        passed = not row.get("error") and answer_score >= case_pass_threshold and citation_ok
        case_results.append(
            {
                "case_id": row["case_id"],
                "question": row["question"],
                "answer": row.get("answer") or "",
                "answer_score": round(answer_score, 4),
                "passed": passed,
                "latency_s": row["latency_s"],
                "ttft_s": row["ttft_s"],
                "trace_id": row.get("trace_id"),
                "citation_count": len(row.get("contexts") or []),
                "sources": row.get("sources") or [],
                "contexts": row.get("contexts") or [],
                "ground_truth": row["ground_truth"],
                "citation_requirement_met": citation_ok,
                "error": row.get("error"),
            }
        )

    history = [
        item for item in _load_history_metrics()
        if item.get("mode") == f"{mode}:{metric_mode}" and item.get("dataset") == dataset
    ]
    previous_metrics = (history[-1].get("metrics") if history else None) or metrics
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    pass_rate = sum(item["passed"] for item in case_results) / len(case_results)
    latencies = [float(item["latency_s"]) for item in case_results]
    ttfts = [
        float(item["ttft_s"]) for item in case_results if item["ttft_s"] is not None
    ]
    payload = {
        "run_id": uuid.uuid4().hex[:12],
        "created_at": created_at,
        "dataset": dataset,
        "version": version,
        "mode": f"{mode}:{metric_mode}",
        "metric_note": "Lexical overlap proxy, not semantic correctness" if not use_ragas else "RAGAS judge",
        "sample_count": len(rows),
        "metrics": metrics,
        "deltas": {
            key: round(
                float(metrics.get(key, 0)) - float(previous_metrics.get(key, 0)), 4
            )
            for key in METRIC_KEYS
        },
        "trend": [
            *[_trend_point(item) for item in history[-4:]],
            _trend_point({"metrics": metrics, "created_at": created_at}),
        ][-5:],
        "version_compare": _build_version_compare(history, metrics),
        "stats": {
            "total_runs": len(history) + 1,
            "question_count": len(rows),
            "avg_latency_s": round(statistics.fmean(latencies), 4),
            "p95_latency_s": round(_p95(latencies), 4),
            "avg_ttft_s": round(statistics.fmean(ttfts), 4) if ttfts else None,
            "pass_rate": round(pass_rate, 4),
            "failed_cases": sum(not item["passed"] for item in case_results),
        },
        "cases": case_results,
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    (REPORTS_DIR / f"{payload['run_id']}.json").write_text(encoded, encoding="utf-8")
    (REPORTS_DIR / "latest.json").write_text(encoded, encoding="utf-8")
    if pass_rate < fail_under:
        raise RuntimeError(
            f"quality gate failed: pass_rate={pass_rate:.4f} < {fail_under:.4f}"
        )
    return payload


def main() -> None:
    global REPORTS_DIR
    parser = argparse.ArgumentParser(description="Run KnowMind RAG evaluation pipeline")
    parser.add_argument("--dataset", default="sample.jsonl")
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--use-ragas", action="store_true")
    parser.add_argument("--version", default="v3")
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("EVAL_API_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument("--token", default=os.getenv("EVAL_ACCESS_TOKEN"))
    parser.add_argument("--email", default=os.getenv("EVAL_EMAIL"))
    parser.add_argument("--password", default=os.getenv("EVAL_PASSWORD"))
    parser.add_argument("--knowledge-base-id", default=os.getenv("EVAL_KB_ID"))
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--case-pass-threshold", type=float, default=0.25)
    parser.add_argument("--fail-under", type=float, default=0.0)
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    args = parser.parse_args()
    REPORTS_DIR = args.reports_dir
    payload = run_eval(
        dataset=args.dataset,
        use_ragas=args.use_ragas,
        version=args.version,
        mode=args.mode,
        api_base_url=args.api_base_url,
        token=args.token,
        email=args.email,
        password=args.password,
        knowledge_base_id=args.knowledge_base_id,
        timeout_s=args.timeout,
        case_pass_threshold=args.case_pass_threshold,
        fail_under=args.fail_under,
    )
    print(
        f"eval complete mode={payload['mode']} samples={payload['sample_count']} "
        f"pass_rate={payload['stats']['pass_rate']:.2%}"
    )
    print(f"wrote reports/latest.json and reports/{payload['run_id']}.json")


if __name__ == "__main__":
    main()
