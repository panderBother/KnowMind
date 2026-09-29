import type { KnowledgeItemDto } from "@/services/knowledgeItems";

export const SOURCE_LABEL: Record<string, string> = {
  manual: "手动",
  document: "文档",
  url: "URL",
  ai_extract: "提炼",
  distill: "蒸馏",
};

/** URL 采集条目正文不可改，以保证与源站一致。 */
export function isKnowledgeItemContentReadonly(item: Pick<KnowledgeItemDto, "source_type">): boolean {
  return item.source_type === "url";
}

/** 所有条目均可删除；关联文档时由后端级联删除文档。 */
export function canDeleteKnowledgeItem(item: Pick<KnowledgeItemDto, "source_type">): boolean {
  void item;
  return true;
}

export type KnowledgeItemDeleteConfirm = {
  title: string;
  message: string;
};

/** 删除确认文案：关联文档时提示将同时删除文档。 */
export function buildKnowledgeItemDeleteConfirm(
  item: Pick<KnowledgeItemDto, "title" | "document_id">,
  documentLabel?: string | null,
): KnowledgeItemDeleteConfirm {
  if (item.document_id) {
    const doc = documentLabel?.trim() || "关联文档";
    return {
      title: "删除条目及关联文档",
      message: `该条目由文档「${doc}」解析生成。删除后将同时移除该文档、上传文件及检索索引，不可恢复。确定继续？`,
    };
  }
  return {
    title: "删除条目",
    message: `确定删除「${item.title}」？此操作不可恢复。`,
  };
}

export function canArchiveKnowledgeItem(item: Pick<KnowledgeItemDto, "source_type" | "lifecycle_status">): boolean {
  return item.lifecycle_status === "published" && item.source_type === "manual";
}
