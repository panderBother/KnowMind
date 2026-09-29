import { partitionThinkingBlocks } from "@/utils/partitionThinking";

/** 报告展示/导出前剥离推理链，只保留 Markdown 正文。 */
export function stripReportMarkdown(raw: string): string {
  const t = partitionThinkingBlocks(raw).visible.trim();
  if (!t) return "";

  const bg = t.match(/(##\s*研究背景[\s\S]*)/);
  if (bg) return bg[1].trim();

  const h2 = t.search(/^##\s+/m);
  if (h2 >= 0) return t.slice(h2).trim();

  const h1 = t.search(/^#\s+/m);
  if (h1 >= 0) return t.slice(h1).trim();

  return t;
}
