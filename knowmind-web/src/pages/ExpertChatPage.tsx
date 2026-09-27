import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Bot, Loader2, MessageSquare, Pencil, Send, Sparkles, SquarePen, Trash2 } from "lucide-react";
import { AssistantMarkdown } from "@/components/AssistantMarkdown";
import { ChatRagSources } from "@/components/ChatRagSources";
import { useUi } from "@/components/ui/UiProvider";
import { getAccessToken } from "@/services/auth";
import type { RagSourceDto } from "@/services/chat";
import {
  type ConversationDto,
  deleteConversation,
  fetchConversation,
  fetchConversationMessages,
  formatConversationLabel,
  getStoredExpertConversationId,
  listExpertConversations,
  setStoredExpertConversationId,
  updateConversationTitle,
} from "@/services/conversations";
import { getExpert, streamExpertChat, type ExpertDto } from "@/services/experts";
import { buildThinkingContent, partitionThinkingBlocks } from "@/utils/partitionThinking";
import { randomId } from "@/utils/randomId";

type Msg = {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinkingContent?: string;
  streamFinal?: boolean;
  ragSources?: RagSourceDto[];
  attachments?: Array<{ id: string; filename: string; file_type: string; size: number }>;
};

export function ExpertChatPage() {
  const { expertId } = useParams<{ expertId: string }>();
  const nav = useNavigate();
  const { confirm, message, prompt } = useUi();
  const bottomRef = useRef<HTMLDivElement>(null);
  const [expert, setExpert] = useState<ExpertDto | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [hydrating, setHydrating] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationDto[]>([]);
  const [loadingConvList, setLoadingConvList] = useState(false);
  const [switchingConv, setSwitchingConv] = useState(false);

  const awaitingFirstToken = useMemo(() => {
    if (!loading) return false;
    const last = messages[messages.length - 1];
    return (
      last?.role === "assistant" &&
      !(last.streamFinal ?? true) &&
      !String(last.content ?? "").trim() &&
      !String(last.thinkingContent ?? "").trim()
    );
  }, [loading, messages]);
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [deepResearch, setDeepResearch] = useState(false);
  const [webSearch, setWebSearch] = useState(false);
  const [arxiv, setArxiv] = useState(false);
  const [semanticScholar, setSemanticScholar] = useState(false);

  const loadConversationList = useCallback(async () => {
    if (!expertId || !getAccessToken()) return;
    setLoadingConvList(true);
    try {
      const rows = await listExpertConversations(expertId, 40);
      setConversations(rows);
    } catch {
      /* 列表失败不阻断对话 */
    } finally {
      setLoadingConvList(false);
    }
  }, [expertId]);

  const applyConversationPayload = useCallback(
    (conv: ConversationDto, msgs: Awaited<ReturnType<typeof fetchConversationMessages>>) => {
      if (!expertId) return;
      setConversationId(conv.id);
      setStoredExpertConversationId(expertId, conv.id);
      setDeepResearch(conv.deep_research);
      setWebSearch(conv.web_search);
      setMessages(
        msgs.map((m) => ({
          id: m.id,
          role: m.role === "assistant" ? "assistant" : "user",
          content: m.content,
          streamFinal: true,
          ragSources: m.citations ?? undefined,
          attachments: m.attachments ?? undefined,
        })),
      );
    },
    [expertId],
  );

  useEffect(() => {
    if (!expertId) return;
    setHydrating(true);
    setExpert(null);
    setMessages([]);
    setConversationId(null);
    setErr(null);
    let cancelled = false;
    void (async () => {
      try {
        const expertRow = await getExpert(expertId);
        if (cancelled) return;
        setExpert(expertRow);
        if (!getAccessToken()) return;
        void loadConversationList();
        const cid = getStoredExpertConversationId(expertId);
        if (!cid) return;
        const [conv, msgs] = await Promise.all([fetchConversation(cid), fetchConversationMessages(cid)]);
        if (cancelled) return;
        if (conv.expert_id && conv.expert_id !== expertId) {
          setStoredExpertConversationId(expertId, null);
          return;
        }
        applyConversationPayload(conv, msgs);
      } catch (e) {
        if (!cancelled) {
          if (getStoredExpertConversationId(expertId)) {
            setStoredExpertConversationId(expertId, null);
          } else {
            setErr(e instanceof Error ? e.message : "加载失败");
          }
        }
      } finally {
        if (!cancelled) setHydrating(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [applyConversationPayload, expertId, loadConversationList]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const newChat = useCallback(() => {
    if (!expertId) return;
    setMessages([]);
    setErr(null);
    setInput("");
    setConversationId(null);
    setStoredExpertConversationId(expertId, null);
    setSessionsOpen(false);
    void loadConversationList();
  }, [expertId, loadConversationList]);

  const selectConversation = useCallback(
    async (id: string) => {
      if (!expertId || loading || switchingConv) return;
      if (id === conversationId) {
        setSessionsOpen(false);
        return;
      }
      setSwitchingConv(true);
      setErr(null);
      setSessionsOpen(false);
      try {
        const [conv, msgs] = await Promise.all([fetchConversation(id), fetchConversationMessages(id)]);
        applyConversationPayload(conv, msgs);
        void loadConversationList();
      } catch {
        setErr("加载会话失败，可能已被删除");
        setStoredExpertConversationId(expertId, null);
        setConversationId(null);
        setMessages([]);
      } finally {
        setSwitchingConv(false);
      }
    },
    [applyConversationPayload, conversationId, expertId, loadConversationList, loading, switchingConv],
  );

  const handleRenameConversation = useCallback(
    async (c: ConversationDto) => {
      const next = await prompt({
        title: "修改会话标题",
        defaultValue: c.title?.trim() || "",
        placeholder: "输入会话标题",
        validate: (value) => (value.trim() ? null : "标题不能为空"),
      });
      if (next === null) return;
      const title = next.trim();
      if (title === (c.title?.trim() || "")) return;
      try {
        const updated = await updateConversationTitle(c.id, title);
        setConversations((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
        message.success("标题已更新");
      } catch (e) {
        const msg = e instanceof Error ? e.message : "修改标题失败";
        setErr(msg);
        message.error(msg);
      }
    },
    [message, prompt],
  );

  const handleDeleteConversation = useCallback(
    async (c: ConversationDto) => {
      if (!expertId) return;
      const label = formatConversationLabel(c);
      const ok = await confirm({
        title: "删除会话",
        message: `确定删除「${label}」？\n对话记录将无法恢复。`,
        confirmText: "删除",
        cancelText: "取消",
        type: "danger",
      });
      if (!ok) return;
      try {
        await deleteConversation(c.id);
        setConversations((rows) => rows.filter((row) => row.id !== c.id));
        if (c.id === conversationId) newChat();
        message.success("会话已删除");
      } catch (e) {
        const msg = e instanceof Error ? e.message : "删除失败";
        setErr(msg);
        message.error(msg);
      }
    },
    [confirm, conversationId, expertId, message, newChat],
  );

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || !expertId || loading) return;
    setInput("");
    setErr(null);
    const assistantId = randomId();
    setMessages((m) => [
      ...m,
      { id: randomId(), role: "user", content: text },
      { id: assistantId, role: "assistant", content: "", streamFinal: false },
    ]);
    setLoading(true);
    let acc = "";
    let thinkingAcc = "";
    try {
      await streamExpertChat(
        expertId,
        {
          message: text,
          deep_research: deepResearch,
          web_search: webSearch,
          arxiv,
          semantic_scholar: semanticScholar,
          conversation_id: conversationId,
        },
        {
          onTraceId: () => undefined,
          onConversationId: (cid) => {
            setConversationId(cid);
            setStoredExpertConversationId(expertId, cid);
          },
          onDelta: (chunk) => {
            acc += chunk;
            setMessages((m) =>
              m.map((row) => {
                if (row.id !== assistantId) return row;
                const { visible } = partitionThinkingBlocks(acc);
                const thinkingContent = buildThinkingContent(thinkingAcc, acc, row.ragSources);
                return { ...row, content: visible, thinkingContent, streamFinal: false };
              }),
            );
          },
          onThinkingDelta: (chunk) => {
            thinkingAcc += chunk;
            setMessages((m) =>
              m.map((row) => {
                if (row.id !== assistantId) return row;
                const { visible } = partitionThinkingBlocks(acc);
                const thinkingContent = buildThinkingContent(thinkingAcc, acc, row.ragSources);
                return { ...row, content: visible, thinkingContent, streamFinal: false };
              }),
            );
          },
          onRagSources: (_kbId, sources) => {
            setMessages((m) =>
              m.map((row) => {
                if (row.id !== assistantId) return row;
                const { visible } = partitionThinkingBlocks(acc);
                const thinkingContent = buildThinkingContent(thinkingAcc, acc, sources);
                return { ...row, ragSources: sources, content: visible, thinkingContent };
              }),
            );
          },
          onError: (msg) => setErr(msg),
          onDone: () => {
            setMessages((m) =>
              m.map((row) => {
                if (row.id !== assistantId) return row;
                const { visible } = partitionThinkingBlocks(acc);
                const thinkingContent = buildThinkingContent(thinkingAcc, acc, row.ragSources);
                return {
                  ...row,
                  content: visible.trim() || row.content,
                  streamFinal: true,
                  thinkingContent: thinkingContent || row.thinkingContent,
                };
              }),
            );
            setLoading(false);
          },
        },
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : "发送失败");
    } finally {
      setLoading(false);
      setMessages((rows) =>
        rows.map((row) =>
          row.id === assistantId ? { ...row, streamFinal: true } : row,
        ),
      );
      void loadConversationList();
    }
  }, [
    arxiv,
    conversationId,
    deepResearch,
    expertId,
    input,
    loadConversationList,
    loading,
    semanticScholar,
    webSearch,
  ]);

  if (hydrating) {
    return (
      <div className="flex flex-1 items-center justify-center gap-2 text-sm text-slate-500">
        <Loader2 className="h-5 w-5 animate-spin text-primary" />
        加载专家…
      </div>
    );
  }

  if (!expert || !expertId) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-sm text-slate-600">
        <p>{err ?? "专家不存在"}</p>
        <button type="button" onClick={() => nav("/experts")} className="text-primary underline">
          返回列表
        </button>
      </div>
    );
  }

  const sessionPanel = (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 pb-2 pt-1">
        <div className="mb-2 flex items-center justify-between px-1">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">历史会话</span>
          {loadingConvList ? <Loader2 className="h-3 w-3 animate-spin text-slate-400" aria-hidden /> : null}
        </div>
        <button
          type="button"
          onClick={newChat}
          disabled={switchingConv}
          className={
            !conversationId
              ? "mb-2 flex w-full items-center gap-2 rounded-lg bg-primary-soft px-2 py-2 text-left text-xs font-medium text-primary"
              : "mb-2 flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-xs text-slate-600 ring-1 ring-slate-100 hover:bg-slate-50"
          }
        >
          <SquarePen className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">新对话</span>
        </button>
        <ul className="space-y-1">
          {conversations.map((c) => (
            <ExpertConversationRow
              key={c.id}
              conversation={c}
              active={c.id === conversationId}
              disabled={switchingConv}
              onSelect={() => void selectConversation(c.id)}
              onRename={() => void handleRenameConversation(c)}
              onDelete={() => void handleDeleteConversation(c)}
            />
          ))}
        </ul>
        {!loadingConvList && conversations.length === 0 ? (
          <p className="px-1 py-3 text-[11px] leading-relaxed text-slate-500">
            暂无历史会话。发送第一条消息后，会话会保存并出现在此列表。
          </p>
        ) : null}
      </div>
    </div>
  );

  return (
    <section className="flex min-h-0 flex-1">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200 bg-slate-50/80 lg:flex xl:w-64">
        <div className="shrink-0 border-b border-slate-200 px-3 py-2.5 text-xs font-semibold text-slate-700">
          {expert.name}
        </div>
        {sessionPanel}
      </aside>

      {sessionsOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-slate-900/40"
            aria-label="关闭会话列表"
            onClick={() => setSessionsOpen(false)}
          />
          <div className="absolute bottom-0 left-0 right-0 flex max-h-[70vh] flex-col rounded-t-2xl border border-slate-200 bg-white shadow-xl">
            <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
              <span className="text-sm font-semibold text-slate-900">历史会话</span>
              <button
                type="button"
                onClick={() => setSessionsOpen(false)}
                className="text-xs text-slate-500 hover:text-slate-800"
              >
                关闭
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-hidden">{sessionPanel}</div>
          </div>
        </div>
      ) : null}

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center gap-3 border-b border-slate-200 bg-white px-4 py-3 lg:px-6">
          <button
            type="button"
            onClick={() => nav("/experts")}
            className="rounded-lg p-1.5 text-slate-600 hover:bg-slate-100"
            aria-label="返回"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <button
            type="button"
            onClick={() => setSessionsOpen(true)}
            className="rounded-lg p-1.5 text-slate-600 hover:bg-slate-100 lg:hidden"
            aria-label="历史会话"
          >
            <MessageSquare className="h-5 w-5" />
          </button>
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-violet-100 text-violet-700">
            <Bot className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-sm font-semibold text-slate-900">{expert.name}</h1>
            <p className="truncate text-[11px] text-slate-500">{expert.description ?? "领域专家对话"}</p>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-600">
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={deepResearch}
                onChange={(e) => setDeepResearch(e.target.checked)}
                className="rounded border-slate-300"
              />
              深度研究
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={webSearch}
                onChange={(e) => setWebSearch(e.target.checked)}
                className="rounded border-slate-300"
              />
              联网
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={arxiv}
                onChange={(e) => setArxiv(e.target.checked)}
                className="rounded border-slate-300"
              />
              arXiv
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={semanticScholar}
                onChange={(e) => setSemanticScholar(e.target.checked)}
                className="rounded border-slate-300"
              />
              Semantic Scholar
            </label>
          </div>
        </header>

        {err ? (
          <div className="mx-4 mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 lg:mx-6">{err}</div>
        ) : null}

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4 lg:px-6">
          {messages.length === 0 ? (
            <div className="mx-auto max-w-2xl rounded-2xl border border-dashed border-violet-200 bg-violet-50/50 p-6 text-center text-sm text-slate-600">
              <p className="font-medium text-slate-800">向 {expert.name} 提问</p>
              <p className="mt-2 text-xs leading-relaxed">
                回答将结合知识库 RAG 检索与专家人设；多轮对话会自动记住上下文，刷新页面后仍可恢复。
              </p>
            </div>
          ) : null}

          {messages.map((m) =>
            m.role === "user" ? (
              <div
                key={m.id}
                className="ml-auto max-w-[90%] rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm text-white lg:max-w-3xl"
              >
                {m.content}
              </div>
            ) : (
              <div
                key={m.id}
                className="max-w-full rounded-2xl border border-slate-200 bg-white p-4 shadow-card lg:max-w-4xl"
              >
                <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
                  <Sparkles className="h-4 w-4 text-violet-600" />
                  {expert.name}
                </div>
                {m.ragSources && m.ragSources.length > 0 ? (
                  <ChatRagSources kbId={expert.kb_id} sources={m.ragSources} />
                ) : null}
                {m.thinkingContent ? (
                  <pre className="mb-3 max-h-40 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-100 bg-slate-50 p-3 text-xs text-slate-600">
                    {m.thinkingContent}
                  </pre>
                ) : null}
                <AssistantMarkdown
                  markdown={m.content}
                  isStreaming={!(m.streamFinal ?? true)}
                  kbId={expert.kb_id}
                  citations={m.ragSources}
                />
              </div>
            ),
          )}
          {awaitingFirstToken ? (
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Loader2 className="h-4 w-4 animate-spin" /> 专家思考中…
            </div>
          ) : null}
          <div ref={bottomRef} />
        </div>

        <footer className="shrink-0 border-t border-slate-200 bg-white p-3 lg:p-4">
          <div className="mx-auto flex max-w-4xl gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void handleSend();
                }
              }}
              rows={2}
              placeholder="输入问题…"
              className="min-h-[44px] flex-1 resize-none rounded-xl border border-slate-200 px-3 py-2 text-sm outline-none focus:border-primary"
            />
            <button
              type="button"
              disabled={loading || !input.trim()}
              onClick={() => void handleSend()}
              className="inline-flex h-11 items-center gap-1 rounded-xl bg-primary px-4 text-sm font-semibold text-white disabled:opacity-50"
            >
              <Send className="h-4 w-4" />
              发送
            </button>
          </div>
        </footer>
      </div>
    </section>
  );
}

function ExpertConversationRow({
  conversation,
  active,
  disabled,
  onSelect,
  onRename,
  onDelete,
}: {
  conversation: ConversationDto;
  active: boolean;
  disabled: boolean;
  onSelect: () => void;
  onRename: () => void;
  onDelete: () => void;
}) {
  const label = formatConversationLabel(conversation);
  const rowClass = active ? "bg-primary-soft text-primary font-medium" : "text-slate-700 hover:bg-slate-50";

  return (
    <li className="group relative">
      <div className={`flex items-start gap-1 rounded-lg px-2 py-2 ${rowClass}`}>
        <button type="button" disabled={disabled} onClick={onSelect} className="min-w-0 flex-1 text-left text-xs">
          <span className="line-clamp-2">{label}</span>
        </button>
        <div className="flex shrink-0 items-center gap-0.5 opacity-100 lg:opacity-0 lg:group-hover:opacity-100 lg:group-focus-within:opacity-100">
          <button
            type="button"
            disabled={disabled}
            onClick={onRename}
            className="rounded p-1 text-slate-400 hover:bg-white/80 hover:text-slate-700"
            aria-label="重命名"
          >
            <Pencil className="h-3 w-3" />
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={onDelete}
            className="rounded p-1 text-slate-400 hover:bg-white/80 hover:text-red-600"
            aria-label="删除"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      </div>
    </li>
  );
}
