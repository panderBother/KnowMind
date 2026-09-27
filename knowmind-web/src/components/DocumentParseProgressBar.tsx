import type { DocumentDto } from "@/services/documents";
import { documentParseProgress, documentParseStageLabel } from "@/services/documents";

type Props = {
  doc: DocumentDto;
  className?: string;
};

/** 文档解析/入库进度条（pending / processing） */
export function DocumentParseProgressBar({ doc, className = "" }: Props) {
  if (doc.status !== "pending" && doc.status !== "processing" && !doc.pending_revision_id) {
    return null;
  }
  const pct = documentParseProgress(doc);
  const label = documentParseStageLabel(doc);

  return (
    <div className={className}>
      <div className="flex items-center justify-between gap-2 text-xs text-slate-500">
        <span>{label ?? "解析中"}</span>
        <span>{pct}%</span>
      </div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className="h-full rounded-full bg-amber-400 transition-all duration-500 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
