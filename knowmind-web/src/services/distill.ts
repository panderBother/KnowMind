import { apiFetch, parseApiError } from "@/services/http";
import type { KnowledgeItemDto } from "@/services/knowledgeItems";

export type KnowledgeGapDto = {
  id: string;
  kb_id: string;
  gap_key: string;
  trigger_rule: string;
  sample_queries: string[];
  avg_score: number | null;
  hit_count: number;
  status: string;
  draft_item_ids: string[] | null;
  created_at: string;
  updated_at: string;
};

export async function listKnowledgeGaps(kbId: string): Promise<KnowledgeGapDto[]> {
  const res = await apiFetch(`/knowledge-bases/${kbId}/distill/gaps`, { });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as KnowledgeGapDto[];
}

export async function analyzeKnowledgeGaps(kbId: string): Promise<KnowledgeGapDto[]> {
  const res = await apiFetch(`/knowledge-bases/${kbId}/distill/analyze`, {
    method: "POST",
    });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as KnowledgeGapDto[];
}

export async function generateGapDrafts(
  kbId: string,
  gapId: string,
): Promise<{ drafts: { id: string; title: string; content: string; lifecycle_status: string }[] }> {
  const res = await apiFetch(`/knowledge-bases/${kbId}/distill/gaps/${gapId}/generate`, {
    method: "POST",
    });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as { drafts: { id: string; title: string; content: string; lifecycle_status: string }[] };
}

export type UrlImportPreviewDto = {
  url: string;
  page_title: string | null;
  title: string;
  summary: string | null;
  content: string;
};

export async function previewUrlItem(kbId: string, url: string): Promise<UrlImportPreviewDto> {
  const res = await apiFetch(`/knowledge-bases/${kbId}/items/preview-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as UrlImportPreviewDto;
}

export async function importUrlItem(
  kbId: string,
  body: {
    url: string;
    category_id: string;
    publish?: boolean;
    title?: string;
    content?: string;
    summary?: string | null;
  },
): Promise<KnowledgeItemDto> {
  const res = await apiFetch(`/knowledge-bases/${kbId}/items/import-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as KnowledgeItemDto;
}

export async function submitChatFeedback(body: {
  knowledge_base_id?: string | null;
  conversation_id?: string | null;
  message_id?: string | null;
  query_text?: string | null;
  correction: string;
}): Promise<void> {
  const res = await apiFetch(`/chat/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
}

export type KnowledgeDraft = { title: string; content: string; tags?: string[] };

export async function extractConversationKnowledge(
  conversationId: string,
  body: { kb_id: string; message_limit?: number },
): Promise<{ drafts: KnowledgeDraft[] }> {
  const res = await apiFetch(`/conversations/${conversationId}/extract-knowledge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as { drafts: KnowledgeDraft[] };
}

export async function importKnowledgeDrafts(
  kbId: string,
  drafts: KnowledgeDraft[],
  publish = false,
): Promise<{ items: { id: string; title: string; lifecycle_status: string }[] }> {
  const res = await apiFetch(`/knowledge-bases/${kbId}/items/import-drafts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ drafts, publish }),
  });
  if (!res.ok) throw new Error(await parseApiError(res));
  return (await res.json()) as { items: { id: string; title: string; lifecycle_status: string }[] };
}
