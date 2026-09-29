/**
 * RAG 评估看板 API（`/api/v1/evaluation/dashboard`）。
 */

import { apiFetch, parseApiError } from "@/services/http";

export type EvalKpiDto = {
  value: number;
  delta: number;
};

export type EvalTrendPointDto = {
  label: string;
  faithfulness: number;
  answer_relevancy: number;
  context_recall: number;
  context_precision: number;
};

export type EvalVersionCompareDto = {
  name: string;
  current: number;
  baseline: number;
};

export type EvalDashboardDto = {
  run_id: string | null;
  created_at: string | null;
  version: string | null;
  mode: string;
  sample_count: number;
  kpis: Record<string, EvalKpiDto>;
  trend: EvalTrendPointDto[];
  version_compare: EvalVersionCompareDto[];
  stats: {
    total_runs: number;
    question_count: number;
    avg_latency_s: number;
    p95_latency_s: number;
    avg_ttft_s: number | null;
    pass_rate: number;
    failed_cases: number;
  };
};

export async function fetchEvalDashboard(): Promise<EvalDashboardDto> {
  const res = await apiFetch(`/evaluation/dashboard`, { });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as EvalDashboardDto;
}

export async function runEvalPipeline(): Promise<EvalDashboardDto> {
  const res = await apiFetch(`/evaluation/run`, {
    method: "POST",
    });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as EvalDashboardDto;
}
