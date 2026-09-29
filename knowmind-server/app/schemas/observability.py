from __future__ import annotations

from pydantic import BaseModel


class ModelUsageOut(BaseModel):
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float


class UsageSummaryOut(BaseModel):
    pricing_configured: bool
    days: int
    calls: int
    completed: int
    failed: int
    stopped: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    avg_latency_ms: int
    p95_latency_ms: int
    models: list[ModelUsageOut]
