from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.memory_constants import approx_token_count
from app.models.orm import AiUsageEvent


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """粗算发送给模型的 Token；供应商未返回 usage 时仍能形成成本趋势。"""
    total = 2
    for message in messages:
        total += 4 + approx_token_count(str(message.get("content") or ""))
    return total


def estimate_cost_microusd(input_tokens: int, output_tokens: int) -> int:
    input_usd = input_tokens * settings.llm_input_cost_per_million_usd / 1_000_000
    output_usd = output_tokens * settings.llm_output_cost_per_million_usd / 1_000_000
    return max(0, round((input_usd + output_usd) * 1_000_000))


def add_usage_event(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
    message_id: str,
    trace_id: str,
    model_mode: str,
    status: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    tool_call_count: int = 0,
    error_type: str | None = None,
) -> AiUsageEvent:
    event = AiUsageEvent(
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        trace_id=trace_id,
        provider="edgefn",
        model=settings.edgefn_chat_model,
        model_mode=model_mode,
        status=status,
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
        total_tokens=max(0, input_tokens) + max(0, output_tokens),
        estimated_cost_microusd=estimate_cost_microusd(input_tokens, output_tokens),
        latency_ms=max(0, latency_ms),
        tool_call_count=max(0, tool_call_count),
        error_type=(error_type or "")[:120] or None,
    )
    session.add(event)
    return event


def _percentile_95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


async def get_usage_summary(
    session: AsyncSession,
    user_id: str,
    *,
    days: int,
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = list(
        (
            await session.scalars(
                select(AiUsageEvent)
                .where(
                    AiUsageEvent.user_id == user_id,
                    AiUsageEvent.created_at >= since,
                )
                .order_by(AiUsageEvent.created_at.desc())
            )
        ).all()
    )

    model_groups: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "input": 0, "output": 0, "total": 0, "cost": 0}
    )
    for row in rows:
        group = model_groups[(row.provider, row.model)]
        group["calls"] += 1
        group["input"] += row.input_tokens
        group["output"] += row.output_tokens
        group["total"] += row.total_tokens
        group["cost"] += row.estimated_cost_microusd

    models = [
        {
            "provider": provider,
            "model": model,
            "calls": totals["calls"],
            "input_tokens": totals["input"],
            "output_tokens": totals["output"],
            "total_tokens": totals["total"],
            "estimated_cost_usd": round(totals["cost"] / 1_000_000, 6),
        }
        for (provider, model), totals in sorted(model_groups.items())
    ]
    latencies = [row.latency_ms for row in rows]
    return {
        "pricing_configured": bool(
            settings.llm_input_cost_per_million_usd or settings.llm_output_cost_per_million_usd
        ),
        "days": days,
        "calls": len(rows),
        "completed": sum(row.status == "completed" for row in rows),
        "failed": sum(row.status == "failed" for row in rows),
        "stopped": sum(row.status == "stopped" for row in rows),
        "input_tokens": sum(row.input_tokens for row in rows),
        "output_tokens": sum(row.output_tokens for row in rows),
        "total_tokens": sum(row.total_tokens for row in rows),
        "estimated_cost_usd": round(
            sum(row.estimated_cost_microusd for row in rows) / 1_000_000,
            6,
        ),
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "p95_latency_ms": _percentile_95(latencies),
        "models": models,
    }
