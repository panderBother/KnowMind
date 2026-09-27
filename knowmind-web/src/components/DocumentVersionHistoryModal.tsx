import { useEffect, useState } from "react";
import { History, RotateCcw, Upload } from "lucide-react";

import { useUi } from "@/components/ui/UiProvider";
import {
  listDocumentVersions,
  retryDocumentVersion,
  rollbackDocumentVersion,
  type DocumentDto,
  type DocumentRevisionDto,
} from "@/services/documents";

type Props = {
  kbId: string;
  doc: DocumentDto;
  onClose: () => void;
  onUploadVersion: (doc: DocumentDto) => void;
  onChanged: () => void;
};

const STATUS_LABEL: Record<string, string> = {
  active: "当前版本",
  superseded: "历史版本",
  pending: "排队中",
  processing: "处理中",
  failed: "失败",
};

export function DocumentVersionHistoryModal({
  kbId,
  doc,
  onClose,
  onUploadVersion,
  onChanged,
}: Props) {
  const { confirm, message } = useUi();
  const [rows, setRows] = useState<DocumentRevisionDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void listDocumentVersions(kbId, doc.id)
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((error: unknown) => {
        if (!cancelled) setErr(error instanceof Error ? error.message : "加载版本历史失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kbId, doc.id, doc.pending_revision_id]);

  const rollback = async (revision: DocumentRevisionDto) => {
    const ok = await confirm({
      title: `回滚到 v${revision.revision_no}`,
      message: "系统会先重建并校验该版本，成功后再切换；当前版本在此期间继续可用。",
      confirmText: "开始回滚",
      type: "warning",
    });
    if (!ok) return;
    setBusyId(revision.id);
    setErr(null);
    try {
      await rollbackDocumentVersion(kbId, doc.id, revision.id);
      message.success(`v${revision.revision_no} 已进入回滚队列`);
      onChanged();
      onClose();
    } catch (error) {
      setErr(error instanceof Error ? error.message : "回滚失败");
    } finally {
      setBusyId(null);
    }
  };

  const retry = async (revision: DocumentRevisionDto) => {
    setBusyId(revision.id);
    setErr(null);
    try {
      await retryDocumentVersion(kbId, doc.id, revision.id);
      message.success(`v${revision.revision_no} 已重新进入处理队列`);
      onChanged();
      onClose();
    } catch (error) {
      setErr(error instanceof Error ? error.message : "重试失败");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/45 p-4" onClick={onClose}>
      <div
        className="flex max-h-[86vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl bg-white shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-3 border-b px-5 py-4">
          <div>
            <div className="flex items-center gap-2">
              <History className="h-5 w-5 text-primary" />
              <h2 className="font-semibold text-slate-900">版本历史 · {doc.filename}</h2>
            </div>
            <p className="mt-1 text-xs text-slate-500">版本更新失败不会影响当前可检索版本。</p>
          </div>
          <button
            type="button"
            disabled={Boolean(doc.pending_revision_id)}
            onClick={() => onUploadVersion(doc)}
            className="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
          >
            <Upload className="h-4 w-4" />
            上传新版本
          </button>
        </div>
        {err ? <p className="bg-red-50 px-5 py-2 text-sm text-red-700">{err}</p> : null}
        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {loading ? (
            <p className="py-10 text-center text-sm text-slate-500">加载版本历史…</p>
          ) : rows.length === 0 ? (
            <p className="py-10 text-center text-sm text-slate-500">暂无版本记录</p>
          ) : (
            <div className="space-y-3">
              {rows.map((revision) => (
                <div key={revision.id} className="rounded-xl border border-slate-200 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="flex items-center gap-2">
                        <strong className="text-sm text-slate-900">v{revision.revision_no}</strong>
                        <span
                          className={
                            revision.is_current
                              ? "rounded-full bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700"
                              : revision.status === "failed"
                                ? "rounded-full bg-red-50 px-2 py-0.5 text-xs text-red-700"
                                : "rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600"
                          }
                        >
                          {revision.is_current ? "当前版本" : STATUS_LABEL[revision.status] ?? revision.status}
                        </span>
                      </div>
                      <p className="mt-1 text-sm text-slate-700">{revision.filename}</p>
                      <p className="mt-1 text-xs text-slate-400">
                        {new Date(revision.created_at).toLocaleString()} · 上传者 {revision.created_by.slice(0, 8)} ·{" "}
                        {revision.chunk_count} 个检索段
                      </p>
                    </div>
                    {!revision.is_current && revision.status === "superseded" ? (
                      <button
                        type="button"
                        disabled={Boolean(doc.pending_revision_id) || busyId === revision.id}
                        onClick={() => void rollback(revision)}
                        className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 disabled:opacity-50"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        {busyId === revision.id ? "提交中…" : "回滚到此版本"}
                      </button>
                    ) : revision.status === "failed" ? (
                      <button
                        type="button"
                        disabled={Boolean(doc.pending_revision_id) || busyId === revision.id}
                        onClick={() => void retry(revision)}
                        className="inline-flex items-center gap-1 rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-700 disabled:opacity-50"
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                        {busyId === revision.id ? "提交中…" : "重试此版本"}
                      </button>
                    ) : null}
                  </div>
                  {revision.status === "failed" && revision.error_message ? (
                    <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700">
                      {revision.error_message}
                    </p>
                  ) : null}
                  {revision.chunk_count > 0 ? (
                    <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                      <span>新增 {revision.added_chunk_count}</span>
                      <span>修改 {revision.changed_chunk_count}</span>
                      <span>复用 {revision.reused_chunk_count}</span>
                      <span>删除 {revision.removed_chunk_count}</span>
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
        <div className="flex justify-end border-t px-5 py-3">
          <button type="button" onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-slate-600">
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
