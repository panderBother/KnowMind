/**
 * 对话 API：同步 `POST /api/v1/chat`；流式 `POST /api/v1/chat/stream`（SSE）。
 */

import { apiFetch, parseApiError } from "@/services/http";

export type ChatToolResult = {
  tool: string;
  ok: boolean;
  result: Record<string, unknown>;
};

/** 对话 RAG 检索命中，用于引用溯源 */
export type RagSourceDto = {
  index: number;
  chunk_id?: string | null;
  item_id?: string | null;
  document_id?: string | null;
  title: string;
  meta?: string | null;
  snippet: string;
  page?: number | null;
  score?: number | null;
  /** 联网 / 学术来源。知识库引用通常没有 URL。 */
  url?: string | null;
  source_type?: "web" | "arxiv" | "semantic_scholar" | string | null;
};

export type ChatRequestBody = {
  message: string;
  knowledge_base_id: string | null;
  deep_research: boolean;
  web_search: boolean;
  model_mode?: "fast" | "balanced" | "deep";
  arxiv?: boolean;
  semantic_scholar?: boolean;
  /** 启用服务端本地文件读写（OpenAI tools） */
  file_tools?: boolean;
  /** 启用已导入且带 url 的远程外部 MCP */
  external_mcp?: boolean;
  attachment_ids?: string[];
  /** 续聊时传入；不传则服务端新建会话并在 SSE 中返回 conversation_id */
  conversation_id?: string | null;
};

export type ChatResponseBody = {
  reply: string;
  trace_id: string;
};

export type AgentStepStatus = "running" | "done" | "error" | "skipped" | "degraded";

export type AgentStepEvent = {
  step: string;
  status: AgentStepStatus;
  detail?: string;
  meta?: Record<string, unknown>;
};

export type ChatStreamHandlers = {
  onTraceId: (traceId: string) => void;
  /** 多轮记忆：服务端新建或确认的会话 id */
  onConversationId?: (conversationId: string, isNew: boolean) => void;
  /** 服务端已持久化本轮助手消息。 */
  onMessageSaved?: (messageId: string) => void;
  /** 服务端已持久化本轮用户消息。 */
  onUserMessageSaved?: (messageId: string) => void;
  /** Agent 编排步骤（RAG、联网、工具等） */
  onAgentStep?: (payload: AgentStepEvent) => void;
  onDelta: (text: string) => void;
  /** 模型推理 / 思维链增量（如 reasoning_content） */
  onThinkingDelta?: (text: string) => void;
  /** 上游业务错误（后端仍会在其后发送 `done`） */
  onError?: (message: string) => void;
  /** 模型调用本地文件读写工具后的结果 */
  onToolResult?: (payload: ChatToolResult) => void;
  /** 知识库 RAG 检索引用来源 */
  onRagSources?: (kbId: string, sources: RagSourceDto[]) => void;
  /** 服务端文件操作日志，如「已执行写入操作」 */
  onFileLog?: (message: string) => void;
  onDone: () => void;
};

async function parseError(res: Response): Promise<string> {
  return parseApiError(res);
}

function chatBodyJson(body: ChatRequestBody): string {
  return JSON.stringify({
    message: body.message,
    knowledge_base_id: body.knowledge_base_id,
    deep_research: body.deep_research,
    web_search: body.web_search,
    model_mode: body.model_mode ?? "balanced",
    arxiv: body.arxiv ?? false,
    semantic_scholar: body.semantic_scholar ?? false,
    file_tools: body.file_tools ?? false,
    external_mcp: body.external_mcp ?? false,
    attachment_ids: body.attachment_ids ?? [],
    conversation_id: body.conversation_id ?? null,
  });
}

export async function sendChatMessage(body: ChatRequestBody): Promise<ChatResponseBody> {
  const res = await apiFetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: chatBodyJson(body),
  });

  if (!res.ok) {
    throw new Error(await parseError(res));
  }
  return (await res.json()) as ChatResponseBody;
}

/**
 * 消费 SSE：`data: {JSON}\n\n`，事件类型 trace_id | delta | done。
 */
export async function streamChatMessage(
  body: ChatRequestBody,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await apiFetch("/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: chatBodyJson(body),
    signal,
  });

  if (!res.ok) {
    throw new Error(await parseError(res));
  }

  const reader = res.body?.getReader();
  if (!reader) {
    throw new Error("响应体不可读");
  }

  const decoder = new TextDecoder();
  let carry = "";

  let doneNotified = false;
  const notifyDone = () => {
    if (doneNotified) return;
    doneNotified = true;
    handlers.onDone();
  };

  const handleLine = (line: string) => {
    if (!line.startsWith("data:")) return;
    const jsonPart = line.slice(5).trimStart();
    if (!jsonPart) return;
    const msg = JSON.parse(jsonPart) as {
      type?: string;
      trace_id?: string;
      text?: string;
      message?: string;
    };
    if (msg.type === "trace_id" && typeof msg.trace_id === "string") {
      handlers.onTraceId(msg.trace_id);
      return;
    }
    if (msg.type === "conversation_id" && typeof (msg as { conversation_id?: string }).conversation_id === "string") {
      const cid = (msg as { conversation_id: string }).conversation_id;
      const isNew = Boolean((msg as { is_new?: boolean }).is_new);
      handlers.onConversationId?.(cid, isNew);
      return;
    }
    if (msg.type === "message_saved" && typeof (msg as { message_id?: string }).message_id === "string") {
      handlers.onMessageSaved?.((msg as { message_id: string }).message_id);
      return;
    }
    if (msg.type === "user_message_saved" && typeof (msg as { message_id?: string }).message_id === "string") {
      handlers.onUserMessageSaved?.((msg as { message_id: string }).message_id);
      return;
    }
    if (msg.type === "agent_step") {
      const step = typeof (msg as { step?: string }).step === "string" ? (msg as { step: string }).step : "";
      const statusRaw = (msg as { status?: string }).status;
      const status =
        statusRaw === "running" || statusRaw === "done" || statusRaw === "error" || statusRaw === "skipped" || statusRaw === "degraded"
          ? statusRaw
          : "done";
      if (step) {
        handlers.onAgentStep?.({
          step,
          status,
          detail: typeof (msg as { detail?: string }).detail === "string" ? (msg as { detail: string }).detail : undefined,
          meta:
            typeof (msg as { meta?: unknown }).meta === "object" && (msg as { meta?: unknown }).meta !== null
              ? ((msg as { meta: Record<string, unknown> }).meta as Record<string, unknown>)
              : undefined,
        });
      }
      return;
    }
    if (msg.type === "thinking_delta" && typeof msg.text === "string") {
      handlers.onThinkingDelta?.(msg.text);
      return;
    }
    if (msg.type === "delta" && typeof msg.text === "string") {
      handlers.onDelta(msg.text);
      return;
    }
    if (msg.type === "error" && typeof msg.message === "string") {
      handlers.onError?.(msg.message);
      return;
    }
    if (msg.type === "file_log" && typeof (msg as { message?: string }).message === "string") {
      handlers.onFileLog?.((msg as { message: string }).message);
      return;
    }
    if (msg.type === "tool_result") {
      const tool = typeof (msg as { tool?: string }).tool === "string" ? (msg as { tool: string }).tool : "";
      const ok = Boolean((msg as { ok?: boolean }).ok);
      const result =
        typeof (msg as { result?: unknown }).result === "object" &&
        (msg as { result?: unknown }).result !== null
          ? ((msg as { result: Record<string, unknown> }).result as Record<string, unknown>)
          : {};
      handlers.onToolResult?.({ tool, ok, result });
      return;
    }
    if (msg.type === "rag_sources") {
      const kbId = typeof (msg as { kb_id?: string }).kb_id === "string" ? (msg as { kb_id: string }).kb_id : "";
      const raw = (msg as { sources?: unknown }).sources;
      if (Array.isArray(raw)) {
        const sources = raw.filter(
          (s): s is RagSourceDto =>
            typeof s === "object" &&
            s !== null &&
            typeof (s as RagSourceDto).index === "number" &&
            typeof (s as RagSourceDto).title === "string" &&
            typeof (s as RagSourceDto).snippet === "string",
        );
        if (sources.length) handlers.onRagSources?.(kbId, sources);
      }
      return;
    }
    if (msg.type === "done") {
      notifyDone();
      return "done" as const;
    }
    return null;
  };

  let finished = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      carry += decoder.decode(value, { stream: true });
      const parts = carry.split("\n");
      carry = parts.pop() ?? "";
      for (const line of parts) {
        if (line.startsWith("data:")) {
          if (handleLine(line) === "done") {
            finished = true;
            break;
          }
        }
      }
      if (finished) break;
    }
    if (!finished && carry.trim()) {
      for (const line of carry.split("\n")) {
        if (line.startsWith("data:")) {
          if (handleLine(line) === "done") {
            finished = true;
            break;
          }
        }
      }
    }
    if (!finished) {
      notifyDone();
    }
  } finally {
    if (!finished) {
      notifyDone();
    }
    if (finished || doneNotified) {
      void reader.cancel().catch(() => undefined);
    }
    reader.releaseLock();
  }
}
