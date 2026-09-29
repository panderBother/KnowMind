from __future__ import annotations

import json

import httpx
import pytest
from pipelines import run_eval as pipeline
from pipelines.run_eval import _jaccard, _parse_sse_line, simple_metrics


def test_chinese_jaccard_uses_cjk_tokens() -> None:
    assert _jaccard("混合检索结合向量和关键词", "向量检索与关键词检索") > 0.25


def test_simple_metrics_never_substitutes_ground_truth_for_answer() -> None:
    rows = [{"question": "q", "ground_truth": "truth", "contexts": ["truth"]}]
    try:
        simple_metrics(rows)
    except ValueError as exc:
        assert "answer is required" in str(exc)
    else:
        raise AssertionError("missing model answer must fail")


def test_parse_sse_line() -> None:
    assert _parse_sse_line('data: {"type":"delta","text":"ok"}') == {
        "type": "delta",
        "text": "ok",
    }
    assert _parse_sse_line("event: message") is None


@pytest.mark.parametrize("failure", [None, "error", "truncated", "http"])
def test_live_requests_use_generated_answer_and_report_failures(tmp_path, monkeypatch, failure):
    dataset = tmp_path / "live.jsonl"
    dataset.write_text(json.dumps({
        "question": "检索方法？", "ground_truth": "混合检索", "answer": "不要使用此离线答案",
        "contexts": ["不要使用离线上下文"], "require_citations": True,
    }), encoding="utf-8")
    monkeypatch.setattr(pipeline, "DATASETS_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "REPORTS_DIR", tmp_path / "reports")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(200, json={"access_token": "test-token"})
        assert request.url.path == "/api/v1/chat/stream"
        assert request.headers["Authorization"] == "Bearer test-token"
        payload = json.loads(request.content)
        assert payload["knowledge_base_id"] == "kb-eval"
        assert "ground_truth" not in payload and "answer" not in payload
        if failure == "http":
            return httpx.Response(503)
        events = [
            {"type": "trace_id", "trace_id": "trace-test"},
            {"type": "rag_sources", "sources": [{"title": "知识库", "snippet": "混合检索"}]},
        ]
        if failure == "error":
            events.append({"type": "error", "message": "upstream unavailable"})
        else:
            events.append({"type": "delta", "text": "混合检索"})
        if failure != "truncated":
            events.append({"type": "done"})
        return httpx.Response(200, text="".join(f"data: {json.dumps(e)}\n\n" for e in events))

    real_client = httpx.Client
    monkeypatch.setattr(pipeline.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler),
    ))
    kwargs = dict(dataset="live.jsonl", use_ragas=False, version="test", mode="live",
                  email="qa@example.com", password="test-only", knowledge_base_id="kb-eval",
                  fail_under=1.0)
    if failure:
        with pytest.raises(RuntimeError, match="quality gate failed"):
            pipeline.run_eval(**kwargs)
    else:
        pipeline.run_eval(**kwargs)
    report = json.loads((tmp_path / "reports" / "latest.json").read_text(encoding="utf-8"))
    assert len(requests) == 2
    assert report["stats"]["pass_rate"] == (0 if failure else 1)
    assert report["cases"][0]["answer"] != "不要使用此离线答案"
    assert "不要使用离线上下文" not in report["cases"][0]["contexts"]
    assert report["cases"][0]["latency_s"] >= 0
    if failure:
        assert report["cases"][0]["error"]


def test_p95_uses_nearest_rank():
    assert pipeline._p95([1, 2, 10]) == 10
