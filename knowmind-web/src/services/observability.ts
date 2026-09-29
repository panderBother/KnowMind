import { apiJson } from "@/services/http";

export type ModelUsageDto = {
  provider: string;
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
};

export type UsageSummaryDto = {
  pricing_configured: boolean;
  days: number;
  calls: number;
  completed: number;
  failed: number;
  stopped: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  models: ModelUsageDto[];
};

export function fetchUsageSummary(days = 7): Promise<UsageSummaryDto> {
  return apiJson<UsageSummaryDto>(`/observability/usage?days=${days}`);
}
