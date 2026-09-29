import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, Download, Pencil, Plug, Trash2, Upload, X } from "lucide-react";
import {
  downloadMcpManifest,
  downloadSkillJson,
  downloadSkillMarkdown,
  fetchSkillJson,
  type SkillExportJsonDto,
} from "@/services/knowledgeExport";
import { listKnowledgeBases, type KnowledgeBaseDto } from "@/services/knowledgeBases";
import {
  deleteCustomMcp,
  fetchMcpTools,
  importMcpJson,
  setBuiltinMcpEnabled,
  setCustomMcpEnabled,
  updateCustomMcp,
  type BuiltinMcpTool,
  type CustomMcpTool,
  type McpServerConfig,
} from "@/services/mcpTools";

function Toggle({
  on,
  disabled,
  onToggle,
  label,
}: {
  on: boolean;
  disabled?: boolean;
  onToggle: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={onToggle}
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
        disabled ? "cursor-not-allowed opacity-40" : "cursor-pointer"
      } ${on ? "bg-primary" : "bg-slate-300"}`}
    >
      <span
        className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
          on ? "translate-x-5" : "translate-x-0"
        }`}
      />
    </button>
  );
}

function ToolCard({
  title,
  description,
  enabled,
  available,
  onToggle,
  badge,
  footer,
}: {
  title: string;
  description: string;
  enabled: boolean;
  available: boolean;
  onToggle: () => void;
  badge?: string;
  footer?: React.ReactNode;
}) {
  return (
    <div
      className={`rounded-2xl border bg-white p-4 shadow-card lg:rounded-xl ${
        enabled ? "border-primary/40 ring-1 ring-primary/10" : "border-slate-200"
      }`}
    >
      <div className="flex items-start gap-3">
        <Plug className={`mt-0.5 h-5 w-5 shrink-0 ${enabled ? "text-primary" : "text-slate-400"}`} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
            <Toggle
              on={enabled}
              disabled={!available}
              onToggle={onToggle}
              label={`${title} 开关`}
            />
          </div>
          {badge ? (
            <span className="mt-1 inline-block rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-medium text-slate-600">
              {badge}
            </span>
          ) : null}
          <p className="mt-2 text-xs leading-relaxed text-slate-500">{description}</p>
          {footer}
        </div>
      </div>
    </div>
  );
}

type CustomMcpMode = "url" | "command";

type CustomMcpFormState = {
  name: string;
  description: string;
  enabled: boolean;
  mode: CustomMcpMode;
  url: string;
  command: string;
  argsText: string;
  cwd: string;
  headersText: string;
  envText: string;
};

function jsonRecordText(value: Record<string, string> | undefined): string {
  const obj = value ?? {};
  if (Object.keys(obj).length === 0) return "";
  return JSON.stringify(obj, null, 2);
}

function parseJsonRecord(text: string, label: string): Record<string, string> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error(`${label} 须为合法 JSON 对象`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} 须为 JSON 对象（键值对）`);
  }
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
    out[String(k)] = v == null ? "" : String(v);
  }
  return out;
}

function toolToFormState(tool: CustomMcpTool): CustomMcpFormState {
  const hasUrl = Boolean((tool.config.url ?? "").trim());
  return {
    name: tool.name,
    description: tool.description,
    enabled: tool.enabled,
    mode: hasUrl ? "url" : "command",
    url: tool.config.url ?? "",
    command: tool.config.command ?? "",
    argsText: (tool.config.args ?? []).join("\n"),
    cwd: tool.config.cwd ?? "",
    headersText: jsonRecordText(tool.config.headers),
    envText: jsonRecordText(tool.config.env),
  };
}

function formStateToConfig(form: CustomMcpFormState): McpServerConfig {
  const headers = parseJsonRecord(form.headersText, "HTTP 请求头");
  const env = parseJsonRecord(form.envText, "环境变量");
  const args = form.argsText
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  if (form.mode === "url") {
    const url = form.url.trim();
    if (!url) throw new Error("请填写远程 URL");
    return {
      url,
      command: null,
      args: [],
      env,
      headers,
      cwd: null,
    };
  }
  const command = form.command.trim();
  if (!command) throw new Error("请填写 command");
  return {
    url: null,
    command,
    args,
    env,
    headers: {},
    cwd: form.cwd.trim() || null,
  };
}

function CustomMcpEditModal({
  tool,
  saving,
  onClose,
  onSave,
}: {
  tool: CustomMcpTool;
  saving: boolean;
  onClose: () => void;
  onSave: (form: CustomMcpFormState) => Promise<void>;
}) {
  const [form, setForm] = useState<CustomMcpFormState>(() => toolToFormState(tool));
  const [localErr, setLocalErr] = useState<string | null>(null);

  useEffect(() => {
    setForm(toolToFormState(tool));
    setLocalErr(null);
  }, [tool]);

  const set = <K extends keyof CustomMcpFormState>(key: K, value: CustomMcpFormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleSubmit = async () => {
    setLocalErr(null);
    try {
      formStateToConfig(form);
      if (!form.name.trim()) throw new Error("请填写服务名称");
      await onSave(form);
    } catch (e) {
      setLocalErr(e instanceof Error ? e.message : "保存失败");
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-slate-900/40 p-4 sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-labelledby="custom-mcp-edit-title"
    >
      <div className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
          <h3 id="custom-mcp-edit-title" className="text-sm font-semibold text-slate-900">
            编辑外部 MCP
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
            aria-label="关闭"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
          {localErr ? (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{localErr}</p>
          ) : null}
          <label className="block text-xs font-medium text-slate-700">
            服务名称
            <input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-primary"
            />
          </label>
          <label className="block text-xs font-medium text-slate-700">
            描述（可选）
            <input
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-primary"
            />
          </label>
          <fieldset className="space-y-2">
            <legend className="text-xs font-medium text-slate-700">连接方式</legend>
            <div className="flex flex-wrap gap-3 text-xs text-slate-700">
              <label className="inline-flex items-center gap-1.5">
                <input
                  type="radio"
                  name={`mode-${tool.id}`}
                  checked={form.mode === "url"}
                  onChange={() => set("mode", "url")}
                />
                远程 URL
              </label>
              <label className="inline-flex items-center gap-1.5">
                <input
                  type="radio"
                  name={`mode-${tool.id}`}
                  checked={form.mode === "command"}
                  disabled
                  onChange={() => undefined}
                />
                本地 command（服务端已禁用）
              </label>
            </div>
          </fieldset>
          {form.mode === "url" ? (
            <>
              <label className="block text-xs font-medium text-slate-700">
                远程 URL
                <input
                  value={form.url}
                  onChange={(e) => set("url", e.target.value)}
                  placeholder="https://api.example.com/mcp"
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-primary"
                />
              </label>
              <label className="block text-xs font-medium text-slate-700">
                HTTP 请求头（JSON）
                <textarea
                  value={form.headersText}
                  onChange={(e) => set("headersText", e.target.value)}
                  rows={4}
                  placeholder={'{\n  "Authorization": "Bearer 你的API_Key"\n}'}
                  className="mt-1 w-full resize-y rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-primary"
                />
              </label>
              <p className="text-[11px] leading-relaxed text-slate-500">
                远程 MCP 常用 <code className="rounded bg-slate-100 px-0.5">Authorization</code> 等请求头；亦可在环境变量里用{" "}
                <code className="rounded bg-slate-100 px-0.5">HEADER_Authorization</code> 写法。
              </p>
            </>
          ) : (
            <>
              <label className="block text-xs font-medium text-slate-700">
                command
                <input
                  value={form.command}
                  onChange={(e) => set("command", e.target.value)}
                  placeholder="uv"
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-primary"
                />
              </label>
              <label className="block text-xs font-medium text-slate-700">
                args（每行一个）
                <textarea
                  value={form.argsText}
                  onChange={(e) => set("argsText", e.target.value)}
                  rows={3}
                  placeholder={"run\npython\n-m\nweb_search.server"}
                  className="mt-1 w-full resize-y rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-primary"
                />
              </label>
              <label className="block text-xs font-medium text-slate-700">
                工作目录 cwd（可选）
                <input
                  value={form.cwd}
                  onChange={(e) => set("cwd", e.target.value)}
                  className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-primary"
                />
              </label>
            </>
          )}
          <label className="block text-xs font-medium text-slate-700">
            环境变量（JSON，可选）
            <textarea
              value={form.envText}
              onChange={(e) => set("envText", e.target.value)}
              rows={3}
              placeholder='{"API_KEY": "..."}'
              className="mt-1 w-full resize-y rounded-lg border border-slate-200 px-3 py-2 font-mono text-xs outline-none focus:border-primary"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2 border-t border-slate-100 px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-slate-200 px-4 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            取消
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={() => void handleSubmit()}
            className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-white disabled:opacity-50"
          >
            {saving ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 工具聚合页：可选内置 MCP + 外部 mcp.json 导入 */
export function ToolsPage() {
  const [builtin, setBuiltin] = useState<BuiltinMcpTool[]>([]);
  const [custom, setCustom] = useState<CustomMcpTool[]>([]);
  const [kbs, setKbs] = useState<KnowledgeBaseDto[]>([]);
  const [exportKbId, setExportKbId] = useState("");
  const [skillPreview, setSkillPreview] = useState<SkillExportJsonDto | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [importing, setImporting] = useState(false);
  const [importNotice, setImportNotice] = useState<string | null>(null);
  const [editingTool, setEditingTool] = useState<CustomMcpTool | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setErr(null);
    setLoading(true);
    try {
      const [data, kbList] = await Promise.all([fetchMcpTools(), listKnowledgeBases()]);
      setBuiltin(data.builtin);
      setCustom(data.custom);
      setKbs(kbList);
      setExportKbId((prev) => prev || kbList[0]?.id || "");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!exportKbId) {
      setSkillPreview(null);
      return;
    }
    void fetchSkillJson(exportKbId)
      .then(setSkillPreview)
      .catch(() => setSkillPreview(null));
  }, [exportKbId]);

  const runExport = async (kind: "skill-md" | "skill-json" | "mcp") => {
    if (!exportKbId) return;
    setExporting(kind);
    setErr(null);
    try {
      if (kind === "skill-md") await downloadSkillMarkdown(exportKbId);
      else if (kind === "skill-json") await downloadSkillJson(exportKbId);
      else await downloadMcpManifest(exportKbId);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "导出失败");
    } finally {
      setExporting(null);
    }
  };

  useEffect(() => {
    void load();
  }, [load]);

  const toggleBuiltin = async (id: string, next: boolean) => {
    try {
      const data = await setBuiltinMcpEnabled(id, next);
      setBuiltin(data.builtin);
      setCustom(data.custom);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "更新失败");
    }
  };

  const toggleCustom = async (id: string, next: boolean) => {
    try {
      const data = await setCustomMcpEnabled(id, next);
      setBuiltin(data.builtin);
      setCustom(data.custom);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "更新失败");
    }
  };

  const handleSaveEdit = async (form: CustomMcpFormState) => {
    if (!editingTool) return;
    setSavingEdit(true);
    setErr(null);
    try {
      const config = formStateToConfig(form);
      const data = await updateCustomMcp(editingTool.id, {
        name: form.name.trim(),
        description: form.description.trim(),
        enabled: editingTool.enabled,
        config,
      });
      setBuiltin(data.builtin);
      setCustom(data.custom);
      setEditingTool(null);
      setImportNotice("外部 MCP 配置已更新");
    } finally {
      setSavingEdit(false);
    }
  };

  const handleImport = async () => {
    if (!importText.trim()) return;
    setImporting(true);
    setErr(null);
    setImportNotice(null);
    try {
      const out = await importMcpJson(importText.trim());
      const parts = [`成功导入 ${out.imported} 个服务`];
      if (out.skipped > 0) {
        parts.push(`跳过 ${out.skipped} 个`);
      }
      if (out.skip_details?.length) {
        parts.push(out.skip_details.join("；"));
      }
      setImportNotice(parts.join("。"));
      setImportText("");
      setImportOpen(false);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "导入失败");
    } finally {
      setImporting(false);
    }
  };

  const handleFilePick = (file: File | null) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === "string" ? reader.result : "";
      setImportText(text);
      setImportOpen(true);
    };
    reader.readAsText(file, "utf-8");
  };

  const exportTemplate = () => {
    const sample = {
      mcpServers: {
        "example-remote": {
          url: "https://example.com/mcp",
          headers: {
            Authorization: "Bearer YOUR_API_KEY",
          },
        },
      },
    };
    const blob = new Blob([JSON.stringify(sample, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "mcp.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-4 pb-6 lg:p-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-900 lg:text-xl">工具与集成</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-600 lg:text-sm">
            选择要在 KnowMind 中启用的能力；外部 MCP 仅允许公网 URL 服务，
            <code className="rounded bg-slate-100 px-1">command</code> / stdio 本地进程已因安全原因禁用。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setImportOpen((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 shadow-sm hover:border-primary/30"
          >
            <Upload className="h-4 w-4" />
            从外部导入
          </button>
          <button
            type="button"
            onClick={exportTemplate}
            className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 shadow-sm"
          >
            <Download className="h-4 w-4" />
            示例 mcp.json
          </button>
        </div>
      </div>

      {err ? (
        <p className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {err}
        </p>
      ) : null}

      {importNotice ? (
        <p className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          {importNotice}
        </p>
      ) : null}

      {importOpen ? (
        <div className="mt-4 rounded-2xl border border-slate-200 bg-slate-50/80 p-4 lg:rounded-xl">
          <p className="text-xs font-medium text-slate-700">
            粘贴 mcp.json（仅导入 <code className="rounded bg-slate-200 px-0.5">url</code> 远程服务；
            command / stdio 配置会被安全策略跳过）
          </p>
          <textarea
            value={importText}
            onChange={(e) => setImportText(e.target.value)}
            rows={8}
            className="mt-2 w-full resize-y rounded-xl border border-slate-200 bg-white p-3 font-mono text-xs text-slate-800 outline-none focus:border-primary"
            placeholder='{"mcpServers": { "my-server": { "url": "https://api.example.com/mcp" } } }'
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={importing || !importText.trim()}
              onClick={() => void handleImport()}
              className="rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-white disabled:opacity-50"
            >
              {importing ? "导入中…" : "确认导入"}
            </button>
            <button
              type="button"
              className="text-xs text-slate-500 underline"
              onClick={() => fileRef.current?.click()}
            >
              或选择本地文件
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(e) => handleFilePick(e.target.files?.[0] ?? null)}
            />
          </div>
        </div>
      ) : null}

      {loading ? (
        <p className="mt-6 text-sm text-slate-500">加载中…</p>
      ) : (
        <>
          <section className="mt-6 rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/5 to-white p-4 shadow-card lg:rounded-xl lg:p-5">
            <div className="flex flex-wrap items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary-soft text-primary">
                <BookOpen className="h-5 w-5" />
              </div>
              <div className="min-w-0 flex-1">
                <h2 className="text-sm font-semibold text-slate-900">Skill / MCP 导出</h2>
                <p className="mt-1 text-xs leading-relaxed text-slate-600">
                  将知识库检索能力导出为 Cursor Skill 或{" "}
                  <code className="rounded bg-white/80 px-1">mcp.json</code> 片段；MCP 配置内含{" "}
                  <code className="rounded bg-white/80 px-1">search_kb</code> 工具，需填入 JWT。
                </p>
              </div>
            </div>

            {kbs.length === 0 ? (
              <p className="mt-4 text-xs text-slate-500">请先创建知识库后再导出。</p>
            ) : (
              <>
                <div className="mt-4 flex flex-wrap items-end gap-3">
                  <label className="flex min-w-[12rem] flex-1 flex-col gap-1 text-xs text-slate-600">
                    选择知识库
                    <select
                      value={exportKbId}
                      onChange={(e) => setExportKbId(e.target.value)}
                      className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-800"
                    >
                      {kbs.map((kb) => (
                        <option key={kb.id} value={kb.id}>
                          {kb.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      disabled={!exportKbId || exporting !== null}
                      onClick={() => void runExport("skill-md")}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-xs font-semibold text-white hover:bg-primary-hover disabled:opacity-50"
                    >
                      <Download className="h-3.5 w-3.5" />
                      {exporting === "skill-md" ? "导出中…" : "SKILL.md"}
                    </button>
                    <button
                      type="button"
                      disabled={!exportKbId || exporting !== null}
                      onClick={() => void runExport("skill-json")}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                    >
                      <Download className="h-3.5 w-3.5" />
                      {exporting === "skill-json" ? "导出中…" : "Skill JSON"}
                    </button>
                    <button
                      type="button"
                      disabled={!exportKbId || exporting !== null}
                      onClick={() => void runExport("mcp")}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                    >
                      <Download className="h-3.5 w-3.5" />
                      {exporting === "mcp" ? "导出中…" : "mcp.json"}
                    </button>
                  </div>
                </div>

                {skillPreview ? (
                  <div className="mt-4 rounded-xl border border-slate-200/80 bg-white/90 p-3">
                    <p className="text-[11px] font-medium text-slate-500">预览（JSON）</p>
                    <pre className="mt-2 max-h-40 overflow-auto font-mono text-[11px] leading-relaxed text-slate-700">
                      {JSON.stringify(
                        {
                          name: skillPreview.name,
                          kb_id: skillPreview.kb_id,
                          kb_name: skillPreview.kb_name,
                          endpoint: skillPreview.endpoint,
                        },
                        null,
                        2,
                      )}
                    </pre>
                    <p className="mt-2 text-[11px] text-slate-500">
                      MCP 导出后请将 <code className="rounded bg-slate-100 px-1">KNOWMIND_ACCESS_TOKEN</code>{" "}
                      替换为登录 Token，并将配置合并到 Cursor 的 mcp.json。
                    </p>
                  </div>
                ) : null}
              </>
            )}
          </section>

          <h2 className="mt-8 text-sm font-semibold text-slate-800">内置工具</h2>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:gap-4">
            {builtin.map((t) => (
              <ToolCard
                key={t.id}
                title={`${t.name} MCP`}
                description={t.description}
                enabled={t.enabled}
                available={t.available}
                onToggle={() => void toggleBuiltin(t.id, !t.enabled)}
                badge={t.available ? (t.enabled ? "已启用" : "已关闭") : "即将接入"}
              />
            ))}
          </div>

          <h2 className="mt-8 text-sm font-semibold text-slate-800">外部导入</h2>
          <p className="mt-1 text-xs text-slate-500">
            支持远程 URL 或本地 command 型 MCP；导入后可点击「编辑」修改 URL、请求头等；对话页打开「外部 MCP」后模型可调用已启用服务的工具。
          </p>
          {custom.length === 0 ? (
            <p className="mt-3 rounded-xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-center text-xs text-slate-500">
              暂无外部 MCP，点击「从外部导入」粘贴配置
            </p>
          ) : (
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:gap-4">
              {custom.map((t) => (
                <ToolCard
                  key={t.id}
                  title={t.name}
                  description={
                    t.config.command
                      ? `command: ${t.config.command} ${(t.config.args ?? []).join(" ")}`.trim()
                      : t.config.url
                        ? `url: ${t.config.url}`
                        : t.description
                  }
                  enabled={t.enabled}
                  available
                  onToggle={() => void toggleCustom(t.id, !t.enabled)}
                  badge={t.enabled ? "已启用" : "已关闭"}
                  footer={
                    <div className="mt-3 flex flex-wrap items-center gap-3">
                      <button
                        type="button"
                        onClick={() => setEditingTool(t)}
                        className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                      >
                        <Pencil className="h-3.5 w-3.5" />
                        编辑
                      </button>
                      <button
                        type="button"
                        onClick={() => void deleteCustomMcp(t.id).then(load)}
                        className="inline-flex items-center gap-1 text-xs text-red-600 hover:underline"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        删除
                      </button>
                    </div>
                  }
                />
              ))}
            </div>
          )}
        </>
      )}
      {editingTool ? (
        <CustomMcpEditModal
          tool={editingTool}
          saving={savingEdit}
          onClose={() => setEditingTool(null)}
          onSave={handleSaveEdit}
        />
      ) : null}
    </div>
  );
}
