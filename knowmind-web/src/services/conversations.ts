/**
 * 多轮对话会话：从后端恢复历史（刷新后保持同一会话）。
 */

import { apiFetch, parseApiError } from "@/services/http";

/** localStorage key，与「新对话」清空逻辑一致 */
export const CHAT_CONVERSATION_STORAGE_KEY = "knowmind_chat_conversation_id";

export type ConversationDto = {
  id: string;
  knowledge_base_id: string | null;
  expert_id?: string | null;
  parent_conversation_id?: string | null;
  branched_from_message_id?: string | null;
  is_pinned: boolean;
  model_mode: "fast" | "balanced" | "deep";
  deep_research: boolean;
  web_search: boolean;
  title: string | null;
  created_at: string;
  updated_at: string;
};

export type ChatMessageDto = {
  id: string;
  role: string;
  content: string;
  trace_id: string | null;
  generation_status: "generating" | "completed" | "failed" | "stopped";
  citations: import("@/services/chat").RagSourceDto[] | null;
  attachments: Array<{ id: string; filename: string; file_type: string; size: number }> | null;
  tool_traces: Array<{
    tool: string;
    ok: boolean;
    result: Record<string, unknown>;
    server?: string;
    mcp_tool?: string;
  }> | null;
  created_at: string;
};

export function formatConversationLabel(c: ConversationDto): string {
  if (c.title && c.title.trim()) return c.title.trim();
  try {
    const d = new Date(c.updated_at);
    return `会话 ${d.toLocaleString(undefined, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
  } catch {
    return `会话 ${c.id.slice(0, 8)}`;
  }
}

export function expertConversationStorageKey(expertId: string): string {
  return `knowmind_expert_conversation_${expertId}`;
}

export function getStoredConversationId(): string | null {
  try {
    return localStorage.getItem(CHAT_CONVERSATION_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setStoredConversationId(id: string | null): void {
  try {
    if (id) localStorage.setItem(CHAT_CONVERSATION_STORAGE_KEY, id);
    else localStorage.removeItem(CHAT_CONVERSATION_STORAGE_KEY);
  } catch {
    /* private mode 等 */
  }
}

export function getStoredExpertConversationId(expertId: string): string | null {
  try {
    return localStorage.getItem(expertConversationStorageKey(expertId));
  } catch {
    return null;
  }
}

export function setStoredExpertConversationId(expertId: string, id: string | null): void {
  try {
    const key = expertConversationStorageKey(expertId);
    if (id) localStorage.setItem(key, id);
    else localStorage.removeItem(key);
  } catch {
    /* private mode 等 */
  }
}

export async function fetchConversation(conversationId: string): Promise<ConversationDto> {
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}`);
  if (!res.ok) {
    throw new Error(res.status === 404 ? "会话不存在" : await res.text());
  }
  return (await res.json()) as ConversationDto;
}

export async function fetchConversationMessages(conversationId: string): Promise<ChatMessageDto[]> {
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}/messages`);
  if (!res.ok) {
    throw new Error(await res.text());
  }
  return (await res.json()) as ChatMessageDto[];
}

/** 智能对话侧栏：不含专家会话 */
export async function listConversations(limit = 50): Promise<ConversationDto[]> {
  const res = await apiFetch(
    `/conversations?limit=${encodeURIComponent(String(limit))}&main_chat_only=true`,
  );
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as ConversationDto[];
}

export type ConversationPage = {
  items: ConversationDto[];
  hasMore: boolean;
};

export async function listConversationPage(options: {
  limit?: number;
  offset?: number;
  query?: string;
} = {}): Promise<ConversationPage> {
  const limit = options.limit ?? 24;
  const params = new URLSearchParams({
    limit: String(limit + 1),
    offset: String(options.offset ?? 0),
    main_chat_only: "true",
  });
  if (options.query?.trim()) params.set("q", options.query.trim());
  const res = await apiFetch(`/conversations?${params.toString()}`);
  if (!res.ok) throw new Error(await parseApiError(res));
  const rows = (await res.json()) as ConversationDto[];
  return { items: rows.slice(0, limit), hasMore: rows.length > limit };
}

/** 某专家下的历史会话 */
export async function listExpertConversations(expertId: string, limit = 40): Promise<ConversationDto[]> {
  const res = await apiFetch(
    `/conversations?limit=${encodeURIComponent(String(limit))}&expert_id=${encodeURIComponent(expertId)}`,
  );
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as ConversationDto[];
}

export async function updateConversationTitle(
  conversationId: string,
  title: string,
): Promise<ConversationDto> {
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as ConversationDto;
}

export async function setConversationPinned(
  conversationId: string,
  isPinned: boolean,
): Promise<ConversationDto> {
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ is_pinned: isPinned }),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as ConversationDto;
}

export async function branchConversation(
  conversationId: string,
  fromMessageId: string,
  title?: string,
): Promise<ConversationDto> {
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}/branch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from_message_id: fromMessageId, title: title || null }),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as ConversationDto;
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const res = await apiFetch(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await parseApiError(res));
}
