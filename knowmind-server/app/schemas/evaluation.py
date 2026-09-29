from pydantic import BaseModel, Field


class EvalKpiOut(BaseModel):
    value: float
    delta: float


class EvalTrendPointOut(BaseModel):
    label: str
    faithfulness: float
    answer_relevancy: float
    context_recall: float
    context_precision: float


class EvalVersionCompareOut(BaseModel):
    name: str
    current: float
    baseline: float


class EvalStatsOut(BaseModel):
    total_runs: int
    question_count: int
    avg_latency_s: float
    p95_latency_s: float = 0.0
    avg_ttft_s: float | None = None
    pass_rate: float
    failed_cases: int = 0


class EvalDashboardOut(BaseModel):
    run_id: str | None = None
    created_at: str | None = None
    version: str | None = None
    mode: str = "stub"
    sample_count: int = 0
    kpis: dict[str, EvalKpiOut] = Field(default_factory=dict)
    trend: list[EvalTrendPointOut] = Field(default_factory=list)
    version_compare: list[EvalVersionCompareOut] = Field(default_factory=list)
    stats: EvalStatsOut = Field(
        default_factory=lambda: EvalStatsOut(
            total_runs=0,
            question_count=0,
            avg_latency_s=0.0,
            pass_rate=0.0,
        )
    )
