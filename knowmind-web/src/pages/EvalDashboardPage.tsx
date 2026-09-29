import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { getAccessToken } from "@/services/auth";
import { fetchEvalDashboard, runEvalPipeline, type EvalDashboardDto } from "@/services/evaluation";
import { fetchUsageSummary, type UsageSummaryDto } from "@/services/observability";

const KPI_META = [
  { key: "faithfulness", title: "忠实度", sub: "Faithfulness" },
  { key: "answer_relevancy", title: "答案相关性", sub: "Answer Relevancy" },
  { key: "context_recall", title: "上下文召回", sub: "Context Recall" },
  { key: "context_precision", title: "上下文精准", sub: "Context Precision" },
] as const;

/**
 * RAG 评估看板：读取 knowmind-eval 流水线产出的 JSON 报告。
 */
export function EvalDashboardPage() {
  const nav = useNavigate();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [data, setData] = useState<EvalDashboardDto | null>(null);
  const [usage, setUsage] = useState<UsageSummaryDto | null>(null);

  const handleRunEval = async () => {
    setRunning(true);
    setError(null);
    try {
      setData(await runEvalPipeline());
    } catch (e) {
      setError(e instanceof Error ? e.message : "评估失败");
    } finally {
      setRunning(false);
    }
  };

  const load = useCallback(async () => {
    if (!getAccessToken()) {
      nav("/login", { replace: true });
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const [evaluation, usageSummary] = await Promise.all([
        fetchEvalDashboard(),
        fetchUsageSummary(7),
      ]);
      setData(evaluation);
      setUsage(usageSummary);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [nav]);

  useEffect(() => {
    void load();
  }, [load]);

  const trendData = useMemo(
    () =>
      (data?.trend ?? []).map((p) => ({
        t: p.label,
        faith: p.faithfulness,
        rel: p.answer_relevancy,
        recall: p.context_recall,
        prec: p.context_precision,
      })),
    [data?.trend],
  );

  const versionData = useMemo(
    () =>
      (data?.version_compare ?? []).map((v) => ({
        name: v.name,
        v21: v.current,
        v20: v.baseline,
      })),
    [data?.version_compare],
  );

  const versionLabel = data?.version ?? "当前";
  const isStub = !data || data.mode === "stub";

  return (
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 pb-6 lg:space-y-6 lg:p-8">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between lg:items-end">
        <div>
          <h1 className="text-lg font-semibold text-slate-900 lg:text-xl">质量与成本</h1>
          <p className="mt-0.5 text-xs text-slate-500 lg:text-sm">
            {isStub
              ? "暂无评估报告，请运行 knowmind-eval 流水线"
              : `${data.mode.includes("ragas") ? "RAGAS 评估" : "词面重叠指标（非语义正确率）"} · ${data.mode} · ${data.sample_count} 样本`}
          </p>
        </div>
        {data?.created_at ? (
          <p className="text-xs text-slate-400">最近评估：{data.created_at.slice(0, 19).replace("T", " ")}</p>
        ) : null}
        <button
          type="button"
          disabled={running}
          onClick={() => void handleRunEval()}
          className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-primary/40 disabled:opacity-50"
        >
          {running ? "评估运行中…" : "运行离线样例回归"}
        </button>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载评估数据…
        </div>
      ) : null}

      {error ? (
        <p className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>
      ) : null}

      {!loading && !error ? (
        <>
          <section className="grid grid-cols-2 gap-3 lg:grid-cols-4 lg:gap-4">
            {KPI_META.map(({ key, title, sub }) => {
              const kpi = data?.kpis[key];
              const value = kpi?.value ?? 0;
              const delta = kpi?.delta ?? 0;
              return (
                <KpiCard
                  key={key}
                  title={title}
                  sub={sub}
                  value={value.toFixed(2)}
                  delta={`${delta >= 0 ? "+" : ""}${delta.toFixed(2)}`}
                  positive={delta >= 0}
                />
              );
            })}
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-card lg:rounded-xl">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold text-slate-900">近 7 天模型用量</h2>
                <p className="mt-0.5 text-xs text-slate-500">按对话生成估算 Token 与成本，不含规划、嵌入及工具循环的全部用量；以供应商账单为准</p>
              </div>
              <span className="text-xs text-slate-400">{usage?.calls ?? 0} 次调用</span>
            </div>
            <dl className="mt-4 grid grid-cols-2 gap-4 text-sm lg:grid-cols-5">
              <UsageStat label="总 Token" value={(usage?.total_tokens ?? 0).toLocaleString()} />
              <UsageStat label="估算成本" value={usage?.pricing_configured ? `$${usage.estimated_cost_usd.toFixed(4)}` : "未配置单价"} />
              <UsageStat label="平均延迟" value={`${usage?.avg_latency_ms ?? 0} ms`} />
              <UsageStat label="P95 延迟" value={`${usage?.p95_latency_ms ?? 0} ms`} />
              <UsageStat label="失败 / 停止" value={`${usage?.failed ?? 0} / ${usage?.stopped ?? 0}`} />
            </dl>
          </section>

          <section className="rounded-2xl border border-slate-200 bg-white p-3 shadow-card lg:rounded-xl lg:p-4">
            <h2 className="text-xs font-semibold text-slate-900 lg:text-sm">指标趋势</h2>
            <div className="mt-3 h-56 lg:mt-4 lg:h-72">
              {trendData.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={trendData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                    <XAxis dataKey="t" tick={{ fontSize: 11 }} />
                    <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Legend />
                    <Line type="monotone" dataKey="faith" name="忠实度" stroke="#0066FF" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="rel" name="答案相关性" stroke="#10B981" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="recall" name="上下文召回" stroke="#F59E0B" strokeWidth={2} dot={false} />
                    <Line type="monotone" dataKey="prec" name="上下文精准" stroke="#6366F1" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              ) : (
                <p className="flex h-full items-center justify-center text-sm text-slate-400">暂无趋势数据</p>
              )}
            </div>
          </section>

          <section className="grid gap-3 lg:grid-cols-2 lg:gap-4">
            <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-card lg:rounded-xl">
              <h2 className="text-sm font-semibold text-slate-900">数据统计</h2>
              <dl className="mt-4 grid grid-cols-2 gap-4 text-sm">
                <div>
                  <dt className="text-xs text-slate-500">总评估次数</dt>
                  <dd className="text-lg font-semibold text-slate-900">
                    {(data?.stats.total_runs ?? 0).toLocaleString()}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">问题数量</dt>
                  <dd className="text-lg font-semibold text-slate-900">{data?.stats.question_count ?? 0}</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">平均响应时延</dt>
                  <dd className="text-lg font-semibold text-slate-900">{data?.stats.avg_latency_s ?? 0}s</dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">P95 / 首 Token</dt>
                  <dd className="text-lg font-semibold text-slate-900">
                    {data?.stats.p95_latency_s ?? 0}s / {data?.stats.avg_ttft_s ?? "—"}s
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-500">通过率</dt>
                  <dd className="text-lg font-semibold text-slate-900">
                    {((data?.stats.pass_rate ?? 0) * 100).toFixed(1)}%
                  </dd>
                </div>
              </dl>
            </div>

            <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-card lg:rounded-xl">
              <h2 className="text-sm font-semibold text-slate-900">
                版本对比（{versionLabel} vs 基线）
              </h2>
              <div className="mt-3 h-52 lg:mt-4 lg:h-64">
                {versionData.length ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={versionData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#E2E8F0" />
                      <XAxis dataKey="name" tick={{ fontSize: 10 }} />
                      <YAxis domain={[0, 100]} tick={{ fontSize: 10 }} />
                      <Tooltip />
                      <Legend />
                      <Bar dataKey="v21" name={versionLabel} fill="#0066FF" radius={[4, 4, 0, 0]} />
                      <Bar dataKey="v20" name="基线" fill="#94A3B8" radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <p className="flex h-full items-center justify-center text-sm text-slate-400">暂无对比数据</p>
                )}
              </div>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

function UsageStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-1 text-lg font-semibold tabular-nums text-slate-900">{value}</dd>
    </div>
  );
}

function KpiCard({
  title,
  sub,
  value,
  delta,
  positive,
}: {
  title: string;
  sub: string;
  value: string;
  delta: string;
  positive?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-3 shadow-card lg:rounded-xl lg:p-4">
      <div className="text-xs font-semibold text-slate-900">{title}</div>
      <div className="text-[10px] text-slate-400">{sub}</div>
      <div className="mt-2 flex items-baseline justify-between gap-2">
        <div className="text-xl font-bold tabular-nums text-slate-900 lg:text-2xl">{value}</div>
        <div className={positive ? "text-xs font-semibold text-emerald-600" : "text-xs font-semibold text-amber-600"}>
          {delta}
        </div>
      </div>
    </div>
  );
}
