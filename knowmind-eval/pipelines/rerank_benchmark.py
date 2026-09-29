"""对固定 RRF 候选集执行 CrossEncoder 前后对照评测。"""

from __future__ import annotations
import argparse
import json
import math
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVER_ENV = ROOT.parent / "knowmind-server" / ".env"


def load_server_env():
    if not SERVER_ENV.is_file():
        return
    for line in SERVER_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load(path: Path):
    return [
        json.loads(x)
        for x in path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]


def dcg(rel):
    return sum(v / math.log2(i + 2) for i, v in enumerate(rel))


def metrics(rows, orders):
    out = {
        "precision@1": [],
        "precision@3": [],
        "precision@5": [],
        "recall@3": [],
        "recall@5": [],
        "mrr": [],
        "ndcg@5": [],
    }
    for row, order in zip(rows, orders):
        gold = {x["id"] for x in row["candidates"] if x.get("relevant")}
        rel = [int(x in gold) for x in order]
        for k in (1, 3, 5):
            out[f"precision@{k}"].append(sum(rel[:k]) / k)
        for k in (3, 5):
            out[f"recall@{k}"].append(sum(rel[:k]) / max(len(gold), 1))
        out["mrr"].append(next((1 / (i + 1) for i, v in enumerate(rel) if v), 0.0))
        ideal = sorted(rel, reverse=True)[:5]
        out["ndcg@5"].append(dcg(rel[:5]) / dcg(ideal) if dcg(ideal) else 0.0)
    return {k: round(sum(v) / len(v), 4) for k, v in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="rerank_benchmark.jsonl")
    ap.add_argument("--model", default="BAAI/bge-reranker-v2-m3")
    ap.add_argument("--backend", choices=("local", "http"), default="http")
    ap.add_argument("--output", default="reports/rerank_benchmark_latest.json")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument(
        "--trust-env", action="store_true", help="使用系统 HTTP(S) 代理环境变量"
    )
    args = ap.parse_args()
    rows = load(ROOT / "datasets" / args.dataset)
    baseline = [[x["id"] for x in r["candidates"]] for r in rows]
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = output.with_suffix(output.suffix + ".checkpoint")
    try:
        cache = (
            json.loads(checkpoint.read_text(encoding="utf-8"))
            if checkpoint.is_file()
            else {}
        )
    except (json.JSONDecodeError, OSError):
        cache = {}
    cache_lock = threading.Lock()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    load_server_env()
    if args.backend == "local":
        from sentence_transformers import CrossEncoder

        model = CrossEncoder(args.model, trust_remote_code=True)
    else:
        model = None
    client = (
        httpx.Client(timeout=120, trust_env=args.trust_env) if model is None else None
    )
    progress = {"done": len(cache)}

    def rerank_one(r):
        cache_key = r["question"]
        if cache_key in cache:
            return cache[cache_key]
        c = r["candidates"]
        if model is not None:
            scores = model.predict(
                [(r["question"], x["text"]) for x in c], show_progress_bar=False
            )
        else:
            base = (
                os.getenv("RERANK_HTTP_BASE_URL")
                or os.getenv("EDGEFN_API_BASE_URL")
                or ""
            ).rstrip("/")
            key = os.getenv("RERANK_HTTP_API_KEY") or os.getenv("EDGEFN_API_KEY") or ""
            if not base or not key:
                raise RuntimeError("缺少云端 Reranker 地址或 API Key")
            name = os.getenv("RERANK_HTTP_MODEL", "bge-reranker-v2-m3")
            for attempt in range(6):
                try:
                    response = client.post(
                        f"{base}/rerank",
                        headers={"Authorization": f"Bearer {key}"},
                        json={
                            "model": name,
                            "query": r["question"],
                            "documents": [x["text"] for x in c],
                        },
                    )
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        wait = (
                            float(retry_after)
                            if retry_after and retry_after.replace(".", "", 1).isdigit()
                            else min(60, 5 * (2**attempt))
                        )
                        print(f"rate limited, retry in {wait:.1f}s", flush=True)
                        time.sleep(wait)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    break
                except Exception:
                    if attempt == 5:
                        raise
                    time.sleep(min(60, 3 * (2**attempt)))
            else:
                raise RuntimeError("Reranker rate limit retry exhausted")
            scores = [0.0] * len(c)
            for item in data["results"]:
                scores[int(item["index"])] = float(
                    item.get("relevance_score", item.get("score", 0))
                )
            if args.delay > 0:
                time.sleep(args.delay)
        order = [
            x["id"]
            for x, _ in sorted(zip(c, scores), key=lambda z: float(z[1]), reverse=True)
        ]
        with cache_lock:
            cache[cache_key] = order
            checkpoint_tmp = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
            checkpoint_tmp.write_text(
                json.dumps(cache, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(checkpoint_tmp, checkpoint)
            progress["done"] += 1
            print(f"progress {progress['done']}/{len(rows)}", flush=True)
        return order

    if model is None and args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            reranked = list(pool.map(rerank_one, rows))
    else:
        reranked = [rerank_one(r) for r in rows]
    if client is not None:
        client.close()
    b, p = metrics(rows, baseline), metrics(rows, reranked)
    delta = {k: round(p[k] - b[k], 4) for k in b}
    rel = {k: round((p[k] - b[k]) / b[k], 4) if b[k] else None for k in b}
    result = {
        "samples": len(rows),
        "baseline_rrf": b,
        "crossencoder": p,
        "absolute_delta": delta,
        "relative_delta": rel,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
