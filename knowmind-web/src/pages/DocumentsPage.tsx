import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  BarChart3,
  CheckCircle2,
  ChevronDown,
  FileText,
  Filter,
  History,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react";

import { KnowledgeItemsPanel } from "@/components/KnowledgeItemsPanel";
import { KnowledgeProductionActions } from "@/components/KnowledgeProductionActions";
import { DocumentImportPreviewModal } from "@/components/DocumentImportPreviewModal";
import { DocumentParseProgressBar } from "@/components/DocumentParseProgressBar";
import { DocumentPreviewDrawer } from "@/components/DocumentPreviewDrawer";
import { DocumentUploadZone } from "@/components/DocumentUploadZone";
import { DocumentVersionHistoryModal } from "@/components/DocumentVersionHistoryModal";
import { useUi } from "@/components/ui/UiProvider";
import { getAccessToken } from "@/services/auth";
import {
  deleteDocument,
  filterSupportedFiles,
  getDocument,
  listDocuments,
  retryDocumentParse,
  SUPPORTED_UPLOAD_ACCEPT,
  uploadDocumentsWithProgress,
  uploadDocumentVersion,
  type DocumentDto,
} from "@/services/documents";
import { listKnowledgeBases, type KnowledgeBaseDto } from "@/services/knowledgeBases";

const VIEW_TABS = ["文档视图", "条目视图"] as const;

/** 与移动原型一致的筛选 Tab */
const TABS_MOBILE = ["全部", "解析中", "待预览", "已完成", "失败"] as const;
const TABS_DESKTOP = ["全部", "处理中", "待处理", "待预览", "已完成", "失败"] as const;

function mapApiStatus(s: string): "Completed" | "Processing" | "Failed" | "Pending" | "Preview" {
  if (s === "done") return "Completed";
  if (s === "processing") return "Processing";
  if (s === "failed") return "Failed";
  if (s === "preview") return "Preview";
  return "Pending";
}

/**
 * 文档上传与管理：选择知识库、上传 PDF、查看解析状态（对接 FastAPI）。
 */
export function DocumentsPage() {
  const nav = useNavigate();
  const location = useLocation();
  const { confirm, message } = useUi();
  const fileRef = useRef<HTMLInputElement>(null);
  const versionFileRef = useRef<HTMLInputElement>(null);
  const [kbs, setKbs] = useState<KnowledgeBaseDto[]>([]);
  const [kbId, setKbId] = useState<string>("");
  const [docs, setDocs] = useState<DocumentDto[]>([]);
  const [viewTab, setViewTab] = useState<(typeof VIEW_TABS)[number]>("文档视图");
  const [tab, setTab] = useState<string>("全部");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [previewDoc, setPreviewDoc] = useState<DocumentDto | null>(null);
  const [previewPage, setPreviewPage] = useState<number | null>(null);
  const [importPreviewDoc, setImportPreviewDoc] = useState<DocumentDto | null>(null);
  const [itemDocFilter, setItemDocFilter] = useState<{ id: string; name: string } | null>(null);
  const [versionUploadDoc, setVersionUploadDoc] = useState<DocumentDto | null>(null);
  const [versionHistoryDoc, setVersionHistoryDoc] = useState<DocumentDto | null>(null);

  const loadKbs = useCallback(async () => {
    if (!getAccessToken()) {
      nav("/login", { replace: true });
      return;
    }
    setErr(null);
    try {
      const rows = await listKnowledgeBases();
      setKbs(rows);
      setKbId((cur) => cur || rows[0]?.id || "");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "加载知识库失败");
    }
  }, [nav]);

  const loadDocs = useCallback(async (options?: { silent?: boolean }) => {
    if (!kbId) return;
    const silent = options?.silent ?? false;
    if (!silent) {
      setLoading(true);
      setErr(null);
    }
    try {
      const rows = await listDocuments(kbId);
      setDocs(rows);
    } catch (e) {
      if (!silent) {
        setErr(e instanceof Error ? e.message : "加载文档失败");
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    void loadKbs();
  }, [loadKbs]);

  useEffect(() => {
    const s = location.state as {
      kbId?: string;
      viewTab?: (typeof VIEW_TABS)[number];
      itemDocFilter?: { id: string; name: string };
    } | null;
    if (!s) return;
    if (s.kbId) setKbId(s.kbId);
    if (s.viewTab) setViewTab(s.viewTab);
    if (s.itemDocFilter) setItemDocFilter(s.itemDocFilter);
    window.history.replaceState({}, document.title);
  }, [location.state]);

  useEffect(() => {
    const sp = new URLSearchParams(location.search);
    const qKbId = sp.get("kbId");
    const docId = sp.get("docId");
    if (!qKbId || !docId) return;
    const pageStr = sp.get("page");
    const page = pageStr != null ? Number.parseInt(pageStr, 10) : null;
    if (qKbId) setKbId(qKbId);
    let cancelled = false;
    void (async () => {
      try {
        const doc = await getDocument(qKbId, docId);
        if (cancelled) return;
        setPreviewDoc(doc);
        setPreviewPage(Number.isFinite(page) ? page : null);
        nav("/documents", { replace: true });
      } catch {
        /* 无效 docId 时忽略 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [location.search, nav]);

  useEffect(() => {
    void loadDocs();
  }, [loadDocs]);

  const hasQueuedDocs = useMemo(
    () =>
      docs.some(
        (d) => d.status === "pending" || d.status === "processing" || Boolean(d.pending_revision_id),
      ),
    [docs],
  );

  /** 有待处理/解析中文档时定时拉状态（Worker 跑完后界面会自动变为已完成） */
  useEffect(() => {
    if (!kbId || !hasQueuedDocs) return;
    const t = window.setInterval(() => void loadDocs({ silent: true }), 2000);
    return () => window.clearInterval(t);
  }, [kbId, hasQueuedDocs, loadDocs]);

  const filtered = useMemo(() => {
    if (tab === "全部") return docs;
    // 移动端「解析中」：排队(pending) 与真正解析(processing) 都归入此类，避免「一直在解析却看不到条目」
    if (tab === "解析中") {
      return docs.filter((d) => d.status === "pending" || d.status === "processing");
    }
    const map: Record<string, string> = {
      处理中: "processing",
      待处理: "pending",
      待预览: "preview",
      已完成: "done",
      失败: "failed",
    };
    const want = map[tab];
    return docs.filter((d) => d.status === want);
  }, [docs, tab]);

  const onRetryParse = async (docId: string) => {
    if (!kbId) return;
    setErr(null);
    try {
      await retryDocumentParse(kbId, docId);
      await loadDocs();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "重试失败");
    }
  };

  const submitDocumentVersion = async (doc: DocumentDto, file: File) => {
    if (!kbId) return;
    setUploading(true);
    setErr(null);
    try {
      const result = await uploadDocumentVersion(kbId, doc.id, file);
      if (result.unchanged) {
        message.info("文件内容与当前版本完全一致，无需更新");
      } else {
        message.success(`v${result.revision?.revision_no ?? ""} 已进入处理队列`);
      }
      setVersionHistoryDoc(null);
      await loadDocs();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "上传新版本失败");
      message.error(e instanceof Error ? e.message : "上传新版本失败");
    } finally {
      setUploading(false);
      setVersionUploadDoc(null);
      if (versionFileRef.current) versionFileRef.current.value = "";
    }
  };

  const openVersionUpload = (doc: DocumentDto) => {
    setVersionUploadDoc(doc);
    window.setTimeout(() => versionFileRef.current?.click(), 0);
  };

  const onPickVersionFile = async (files: FileList | null) => {
    const file = files?.[0];
    if (!file || !versionUploadDoc) return;
    const supported = filterSupportedFiles(files);
    if (!supported.length) {
      setErr("请选择支持的文件格式");
      return;
    }
    await submitDocumentVersion(versionUploadDoc, file);
  };

  const uploadFiles = async (files: File[]) => {
    if (!kbId || !files.length) return;
    if (files.length === 1) {
      const sameName = docs.find(
        (doc) => doc.status === "done" && doc.filename.toLowerCase() === files[0].name.toLowerCase(),
      );
      if (sameName) {
        const replace = await confirm({
          title: "检测到同名文档",
          message: `知识库中已有「${sameName.filename}」。选择“更新已有”会保留版本历史；选择取消则作为新文档上传。`,
          confirmText: "更新已有",
          cancelText: "作为新文档上传",
          type: "info",
        });
        if (replace) {
          await submitDocumentVersion(sameName, files[0]);
          return;
        }
      }
    }
    setUploading(true);
    setUploadProgress(0);
    setErr(null);
    try {
      const result = await uploadDocumentsWithProgress(kbId, files, setUploadProgress);
      await loadDocs();
      const previewId = result.needs_preview[0];
      if (previewId) {
        const doc = result.documents.find((d) => d.id === previewId);
        if (doc) setImportPreviewDoc(doc);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "上传失败");
    } finally {
      setUploading(false);
      setUploadProgress(0);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const onPickFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    const arr = filterSupportedFiles(files);
    if (!arr.length) {
      setErr("请选择支持的文件格式（PDF、DOCX、Excel、CSV、Markdown、TXT、图片等）");
      return;
    }
    await uploadFiles(arr);
  };

  const onEditDocumentItems = (doc: DocumentDto) => {
    setPreviewDoc(null);
    setPreviewPage(null);
    setItemDocFilter({ id: doc.id, name: doc.filename });
    setViewTab("条目视图");
  };

  const openPreview = (doc: DocumentDto) => {
    if (doc.status === "preview") {
      setImportPreviewDoc(doc);
      return;
    }
    setPreviewDoc(doc);
  };

  const onDeleteDocument = async (doc: DocumentDto) => {
    if (!kbId) return;
    const itemHint =
      doc.status === "done"
        ? "将同时删除对应知识条目与检索索引。"
        : "若已有知识条目，也会一并删除。";
    const ok = await confirm({
      title: "删除文档",
      message: `确定删除「${doc.filename}」？${itemHint}此操作不可恢复。`,
      confirmText: "删除",
      type: "danger",
    });
    if (!ok) return;
    setErr(null);
    try {
      await deleteDocument(kbId, doc.id);
      if (previewDoc?.id === doc.id) setPreviewDoc(null);
      if (itemDocFilter?.id === doc.id) setItemDocFilter(null);
      message.success("文档及关联条目已删除");
      await loadDocs();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "删除失败");
    }
  };

  const kbName = kbs.find((k) => k.id === kbId)?.name ?? "选择知识库";

  return (
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 lg:space-y-6 lg:p-8">
      <input
        ref={versionFileRef}
        type="file"
        accept={SUPPORTED_UPLOAD_ACCEPT}
        className="hidden"
        onChange={(event) => void onPickVersionFile(event.target.files)}
      />
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-900 lg:text-xl">文档管理</h1>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {kbId ? (
            <button
              type="button"
              onClick={() => nav(`/knowledge-bases/${kbId}/analytics`)}
              className="hidden items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 hover:border-primary/40 hover:text-primary lg:inline-flex"
            >
              <BarChart3 className="h-3.5 w-3.5" />
              使用热度
            </button>
          ) : null}
          <button
            type="button"
            className="rounded-full border border-slate-200 bg-white p-2 text-slate-500 shadow-sm lg:hidden"
            aria-label="筛选"
          >
            <Filter className="h-5 w-5" />
          </button>
        </div>
      </div>

      {err ? (
        <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
          {err}
        </p>
      ) : null}

      {kbId ? <KnowledgeProductionActions kbId={kbId} className="mb-2" /> : null}

      <div className="flex gap-1 rounded-xl border border-slate-200 bg-white p-1 shadow-sm">
        {VIEW_TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setViewTab(t)}
            className={
              viewTab === t
                ? "flex-1 rounded-lg bg-primary-soft py-2 text-sm font-semibold text-primary"
                : "flex-1 rounded-lg py-2 text-sm text-slate-600 hover:bg-slate-50"
            }
          >
            {t}
          </button>
        ))}
      </div>

      {viewTab === "条目视图" ? (
        kbId ? (
          <KnowledgeItemsPanel
            kbId={kbId}
            documentId={itemDocFilter?.id}
            documentName={itemDocFilter?.name}
            onClearDocumentFilter={() => setItemDocFilter(null)}
          />
        ) : (
          <p className="text-sm text-slate-500">请先选择知识库</p>
        )
      ) : (
        <>
      <section className="space-y-3 lg:space-y-4">
        <div className="hidden lg:block">
            <label className="mb-1 block text-xs font-medium text-slate-600">目标知识库</label>
            <div className="relative max-w-md">
              <select
                className="w-full appearance-none rounded-lg border border-slate-200 bg-white px-3 py-2 pr-9 text-left text-sm text-slate-800"
                value={kbId}
                onChange={(e) => setKbId(e.target.value)}
              >
                <option value="" disabled>
                  请选择
                </option>
                {kbs.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.name}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-3 top-2.5 h-4 w-4 text-slate-400" />
            </div>
          </div>

          <div className="lg:hidden">
            <label className="mb-1 block text-xs font-medium text-slate-600">知识库</label>
            <select
              className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
              value={kbId}
              onChange={(e) => setKbId(e.target.value)}
            >
              <option value="" disabled>
                请选择
              </option>
              {kbs.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.name}
                </option>
              ))}
            </select>
          </div>

          <input
            ref={fileRef}
            type="file"
            accept={SUPPORTED_UPLOAD_ACCEPT}
            multiple
            className="hidden"
            onChange={(e) => void onPickFiles(e.target.files)}
          />

          <DocumentUploadZone
            kbName={kbName}
            disabled={!kbId}
            uploading={uploading}
            uploadProgress={uploadProgress}
            onBrowseClick={() => fileRef.current?.click()}
            onSelectFiles={uploadFiles}
          />
      </section>

      <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-card lg:rounded-xl">
        <div className="flex gap-1 overflow-x-auto border-b border-slate-100 px-2 py-2 lg:flex-wrap lg:px-4 lg:py-3">
          <div className="flex gap-1 lg:hidden">
            {TABS_MOBILE.map((t) => (
              <TabBtn key={t} t={t} tab={tab} setTab={setTab} />
            ))}
          </div>
          <div className="hidden gap-2 lg:flex lg:flex-wrap">
            {TABS_DESKTOP.map((t) => (
              <TabBtn key={t} t={t} tab={tab} setTab={setTab} />
            ))}
          </div>
        </div>

        <ul className="divide-y divide-slate-100 lg:hidden">
          {loading ? (
            <li className="p-4 text-sm text-slate-500">加载中…</li>
          ) : (
            filtered.map((row) => {
              const st = mapApiStatus(row.status);
              const mb = (row.file_bytes / (1024 * 1024)).toFixed(2);
              const title = row.title || row.filename;
              return (
                <li key={row.id} className="flex gap-3 p-4">
                  <button
                    type="button"
                    onClick={() => openPreview(row)}
                    className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-red-50 text-red-600 hover:bg-red-100"
                  >
                    <FileText className="h-6 w-6" />
                  </button>
                  <button
                    type="button"
                    onClick={() => openPreview(row)}
                    className="min-w-0 flex-1 text-left hover:opacity-90"
                  >
                    <div className="font-medium leading-snug text-slate-900">{row.filename}</div>
                    <div className="mt-0.5 text-xs text-slate-500">
                      {mb} MB · {title.slice(0, 40)}
                      {title.length > 40 ? "…" : ""}
                    </div>
                    <DocumentParseProgressBar doc={row} className="mt-2" />
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <StatusBadge status={st} />
                    </div>
                    {row.error_message ? (
                      <p className="mt-1 text-xs text-red-600">{row.error_message}</p>
                    ) : null}
                  </button>
                  <div className="flex shrink-0 flex-col items-end gap-2">
                    {st === "Completed" && <CheckCircle2 className="h-5 w-5 text-emerald-500" />}
                    {st === "Failed" && <XCircle className="h-5 w-5 text-red-500" />}
                    {(row.status === "pending" ||
                      row.status === "processing" ||
                      row.status === "failed") && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          void onRetryParse(row.id);
                        }}
                        className="text-xs font-semibold text-primary hover:underline"
                      >
                        重新解析
                      </button>
                    )}
                    {row.status === "preview" ? (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          setImportPreviewDoc(row);
                        }}
                        className="text-xs font-semibold text-primary hover:underline"
                      >
                        预览确认
                      </button>
                    ) : null}
                    {row.status === "done" ? (
                      <>
                        <button
                          type="button"
                          disabled={Boolean(row.pending_revision_id)}
                          onClick={(event) => {
                            event.stopPropagation();
                            openVersionUpload(row);
                          }}
                          className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline disabled:opacity-50"
                        >
                          <Upload className="h-3.5 w-3.5" />
                          新版本
                        </button>
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            setVersionHistoryDoc(row);
                          }}
                          className="inline-flex items-center gap-1 text-xs font-semibold text-slate-600 hover:underline"
                        >
                          <History className="h-3.5 w-3.5" />
                          版本
                        </button>
                        <button
                          type="button"
                          onClick={() => onEditDocumentItems(row)}
                          className="text-xs font-semibold text-primary hover:underline"
                        >
                          编辑内容
                        </button>
                      </>
                    ) : null}
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        void onDeleteDocument(row);
                      }}
                      className="inline-flex items-center gap-0.5 text-xs font-semibold text-red-600 hover:underline"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      删除
                    </button>
                  </div>
                </li>
              );
            })
          )}
        </ul>

        <div className="hidden overflow-x-auto lg:block">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-50 text-xs uppercase text-slate-500">
              <tr>
                <th className="px-4 py-3">文件名</th>
                <th className="px-4 py-3">标题</th>
                <th className="px-4 py-3">大小</th>
                <th className="px-4 py-3">检索段</th>
                <th className="px-4 py-3">状态</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {loading ? (
                <tr>
                  <td colSpan={5} className="px-4 py-6 text-slate-500">
                    加载中…
                  </td>
                </tr>
              ) : (
                filtered.map((row) => {
                  const st = mapApiStatus(row.status);
                  const mb = (row.file_bytes / (1024 * 1024)).toFixed(2);
                  return (
                    <tr
                      key={row.id}
                      className="cursor-pointer hover:bg-slate-50/80"
                      onClick={() => openPreview(row)}
                    >
                      <td className="px-4 py-3 font-medium text-primary hover:underline">{row.filename}</td>
                      <td className="px-4 py-3 text-slate-600">{row.title || "—"}</td>
                      <td className="px-4 py-3 text-slate-600">{mb} MB</td>
                      <td className="px-4 py-3 text-slate-600">{row.chunk_count}</td>
                      <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                        <div className="flex flex-col gap-1.5">
                          <div className="flex flex-wrap items-center gap-2">
                          <StatusBadge status={st} />
                          {(row.status === "pending" ||
                            row.status === "processing" ||
                            row.status === "failed") && (
                            <button
                              type="button"
                              onClick={() => void onRetryParse(row.id)}
                              className="text-xs font-semibold text-primary hover:underline"
                            >
                              重新解析
                            </button>
                          )}
                          {row.status === "preview" ? (
                            <button
                              type="button"
                              onClick={() => setImportPreviewDoc(row)}
                              className="text-xs font-semibold text-primary hover:underline"
                            >
                              预览确认
                            </button>
                          ) : null}
                          {row.status === "done" ? (
                            <>
                              <button
                                type="button"
                                disabled={Boolean(row.pending_revision_id)}
                                onClick={() => openVersionUpload(row)}
                                className="text-xs font-semibold text-primary hover:underline disabled:opacity-50"
                              >
                                上传新版本
                              </button>
                              <button
                                type="button"
                                onClick={() => setVersionHistoryDoc(row)}
                                className="text-xs font-semibold text-slate-600 hover:underline"
                              >
                                版本历史
                              </button>
                              <button
                                type="button"
                                onClick={() => onEditDocumentItems(row)}
                                className="text-xs font-semibold text-primary hover:underline"
                              >
                                编辑内容
                              </button>
                            </>
                          ) : null}
                          <button
                            type="button"
                            onClick={() => void onDeleteDocument(row)}
                            className="inline-flex items-center gap-0.5 text-xs font-semibold text-red-600 hover:underline"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                            删除
                          </button>
                        </div>
                        <DocumentParseProgressBar doc={row} />
                      </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="flex items-center justify-between border-t border-slate-100 px-4 py-3 text-xs text-slate-500">
          <span>共 {filtered.length} 条</span>
        </div>
      </section>
        </>
      )}

      {importPreviewDoc && kbId ? (
        <DocumentImportPreviewModal
          kbId={kbId}
          doc={importPreviewDoc}
          onClose={() => setImportPreviewDoc(null)}
          onConfirmed={() => {
            setImportPreviewDoc(null);
            void loadDocs();
          }}
        />
      ) : null}

      {previewDoc && kbId ? (
        <DocumentPreviewDrawer
          kbId={kbId}
          doc={previewDoc}
          initialPage={previewPage}
          onClose={() => {
            setPreviewDoc(null);
            setPreviewPage(null);
          }}
          onEditItems={onEditDocumentItems}
          onDelete={(doc) => void onDeleteDocument(doc)}
        />
      ) : null}

      {versionHistoryDoc && kbId ? (
        <DocumentVersionHistoryModal
          kbId={kbId}
          doc={versionHistoryDoc}
          onClose={() => setVersionHistoryDoc(null)}
          onUploadVersion={openVersionUpload}
          onChanged={() => void loadDocs()}
        />
      ) : null}
    </div>
  );
}

function TabBtn({
  t,
  tab,
  setTab,
}: {
  t: string;
  tab: string;
  setTab: (s: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => setTab(t)}
      className={
        t === tab
          ? "shrink-0 rounded-full bg-primary-soft px-3 py-1.5 text-xs font-semibold text-primary"
          : "shrink-0 rounded-full px-3 py-1.5 text-xs text-slate-500 hover:bg-slate-50"
      }
    >
      {t}
    </button>
  );
}

function StatusBadge({
  status,
}: {
  status: "Completed" | "Processing" | "Failed" | "Pending" | "Preview";
}) {
  const cls =
    status === "Completed"
      ? "bg-emerald-50 text-emerald-700"
      : status === "Processing"
        ? "bg-amber-50 text-amber-700"
        : status === "Preview"
          ? "bg-sky-50 text-sky-700"
        : status === "Failed"
          ? "bg-red-50 text-red-700"
          : "bg-slate-100 text-slate-600";
  const label =
    status === "Completed"
      ? "已完成"
      : status === "Processing"
        ? "解析中"
        : status === "Preview"
          ? "待预览"
        : status === "Failed"
          ? "失败"
          : "排队中";
  return <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}>{label}</span>;
}
