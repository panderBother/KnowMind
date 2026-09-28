import { ChevronDown, Database, ExternalLink, Globe2, Wrench } from "lucide-react";
import { useState } from "react";

import { ChatRagSources } from "@/components/ChatRagSources";
import type { RagSourceDto } from "@/services/chat";

type ToolLog = { tool: string; ok: boolean; summary: string };

export function ChatSourcePanel({
  kbId,
  sources = [],
  toolLogs = [],
}: {
  kbId?: string;
  sources?: RagSourceDto[];
  toolLogs?: ToolLog[];
}) {
  const [open, setOpen] = useState(false);
  const localSources = sources.filter((source) => !source.url);
  const externalSources = sources.filter((source) => Boolean(source.url));
  const total = sources.length + toolLogs.length;
  if (!total) return null;

  return (
    <section className="mb-3 overflow-hidden rounded-xl bg-slate-50 ring-1 ring-inset ring-slate-200/80">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-slate-700 transition hover:bg-slate-100/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/30"
        aria-expanded={open}
      >
        <Database className="h-3.5 w-3.5 text-primary" />
        <span>来源与工具</span>
        <span className="text-[11px] font-normal text-slate-400">
          {sources.length ? `${sources.length} 条引用` : ""}
          {sources.length && toolLogs.length ? " · " : ""}
          {toolLogs.length ? `${toolLogs.length} 次调用` : ""}
        </span>
        <ChevronDown className={`ml-auto h-3.5 w-3.5 transition ${open ? "rotate-180" : ""}`} />
      </button>
      {open ? (
        <div className="border-t border-slate-200/80 p-2.5">
          {localSources.length && kbId ? (
            <ChatRagSources kbId={kbId} sources={localSources} />
          ) : null}
          {externalSources.length ? (
            <div className="mb-3 rounded-lg bg-white p-2.5 ring-1 ring-inset ring-slate-200">
              <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold text-slate-600">
                <Globe2 className="h-3.5 w-3.5" /> 联网与学术来源
              </p>
              <ul className="space-y-2">
                {externalSources.map((source) => (
                  <li key={`${source.source_type}-${source.index}-${source.url}`}>
                    <a
                      href={source.url ?? undefined}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="block rounded-lg border border-slate-100 p-2 text-xs transition hover:border-primary/30 hover:bg-slate-50"
                    >
                      <span className="flex items-start gap-2 font-medium text-slate-800">
                        <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-bold text-primary">
                          [{source.index}]
                        </span>
                        <span className="min-w-0 flex-1">{source.title}</span>
                        <ExternalLink className="mt-0.5 h-3 w-3 shrink-0 text-slate-400" />
                      </span>
                      {source.snippet ? (
                        <span className="mt-1 block line-clamp-2 pl-7 text-[11px] leading-relaxed text-slate-500">
                          {source.snippet}
                        </span>
                      ) : null}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {toolLogs.length ? (
            <div className="rounded-lg bg-white p-2.5 ring-1 ring-inset ring-slate-200">
              <p className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold text-slate-600">
                <Wrench className="h-3.5 w-3.5" /> 工具轨迹
              </p>
              <ul className="space-y-1.5 text-xs">
                {toolLogs.map((log, index) => (
                  <li key={`${log.tool}-${index}`} className="flex gap-2">
                    <span className={log.ok ? "text-emerald-600" : "text-red-600"}>
                      {log.ok ? "完成" : "失败"}
                    </span>
                    <span className="font-mono text-[11px] text-slate-600">{log.tool}</span>
                    <span className="min-w-0 break-all text-slate-500">{log.summary}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
