export type ChatMediaItem = {
  type: "image" | "video";
  url: string;
};

const IMAGE_EXT = /\.(?:png|jpe?g|webp|gif|bmp|svg|avif|heic)(?:$|[?#])/i;
const VIDEO_EXT = /\.(?:mp4|webm|mov|mkv|avi|m3u8|ogv)(?:$|[?#])/i;

const MARKDOWN_LINK = /\[([^\]\n]*)\]\((https?:\/\/[^\s)]+)\)/g;
const MARKDOWN_IMAGE = /!\[([^\]\n]*)\]\((https?:\/\/[^\s)]+)\)/g;
const BARE_URL = /https?:\/\/[^\s<>"\])]+/g;

const MEDIA_FIELD_KEYS = new Set([
  "url",
  "href",
  "link",
  "uri",
  "image_url",
  "imageurl",
  "image",
  "video_url",
  "videourl",
  "video",
  "output_url",
  "outputurl",
  "file_url",
  "fileurl",
  "download_url",
  "downloadurl",
  "src",
  "media_urls",
]);

function classifyMediaUrl(url: string): ChatMediaItem["type"] | null {
  const trimmed = url.trim();
  if (trimmed.startsWith("data:image/")) return "image";
  if (trimmed.startsWith("data:video/")) return "video";
  if (!trimmed.startsWith("http")) return null;
  if (IMAGE_EXT.test(trimmed)) return "image";
  if (VIDEO_EXT.test(trimmed)) return "video";
  return null;
}

function addUrl(url: string, out: Map<string, ChatMediaItem>) {
  const kind = classifyMediaUrl(url);
  if (!kind || out.has(url)) return;
  out.set(url, { type: kind, url });
}

function collectFromString(text: string, out: Map<string, ChatMediaItem>) {
  for (const match of text.matchAll(MARKDOWN_IMAGE)) {
    addUrl(match[2], out);
  }
  for (const match of text.matchAll(MARKDOWN_LINK)) {
    addUrl(match[2], out);
  }
  for (const match of text.matchAll(BARE_URL)) {
    addUrl(match[0].replace(/[.,;:!?)]+$/, ""), out);
  }
}

function walkValue(value: unknown, out: Map<string, ChatMediaItem>) {
  if (value == null) return;
  if (typeof value === "string") {
    collectFromString(value, out);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) walkValue(item, out);
    return;
  }
  if (typeof value === "object") {
    for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
      if (key.toLowerCase() === "media_urls" && Array.isArray(nested)) {
        for (const item of nested) {
          if (typeof item === "string") addUrl(item, out);
        }
      }
      if (MEDIA_FIELD_KEYS.has(key.toLowerCase()) && typeof nested === "string") {
        addUrl(nested, out);
      }
      walkValue(nested, out);
    }
  }
}

/** 从 MCP / 文件工具返回的 result 对象中提取可展示的图片、视频 URL */
export function extractMediaFromToolResult(result: Record<string, unknown>): ChatMediaItem[] {
  const out = new Map<string, ChatMediaItem>();
  walkValue(result, out);
  return [...out.values()];
}

/** 从助手 Markdown 正文中提取图片 / 视频 URL（模型总结或 MCP 文本里常见） */
export function extractMediaFromText(text: string): ChatMediaItem[] {
  const out = new Map<string, ChatMediaItem>();
  if (!text.trim()) return [];
  collectFromString(text, out);
  return [...out.values()];
}

/** 合并工具结果与正文中的媒体，按 URL 去重 */
export function mergeChatMedia(
  fromTools: ChatMediaItem[] | undefined,
  content: string,
): ChatMediaItem[] {
  const out = new Map<string, ChatMediaItem>();
  for (const item of fromTools ?? []) out.set(item.url, item);
  for (const item of extractMediaFromText(content)) out.set(item.url, item);
  return [...out.values()];
}

const IMAGE_URL_IN_PARENS = /!\[[^\]]*\]\((https?:\/\/[^\s)]+)\)|\[(?!!\[)[^\]]*\]\((https?:\/\/[^\s)]+)\)/g;

function isMediaUrl(url: string): boolean {
  return classifyMediaUrl(url) !== null;
}

/** 正文中已用画廊展示的图/视频链接，从 Markdown 里去掉，避免重复显示成文字链接 */
export function stripMediaFromAssistantContent(text: string): string {
  let out = text.replace(/\r\n/g, "\n");
  out = out.replace(IMAGE_URL_IN_PARENS, (full, u1, u2) => {
    const url = u1 || u2;
    return url && isMediaUrl(url) ? "" : full;
  });
  out = out.replace(BARE_URL, (url) => (isMediaUrl(url) ? "" : url));
  return out.replace(/\n{3,}/g, "\n\n").trimEnd();
}

const MCP_MEDIA_REPLY_RE =
  /generate_image|nanobanana|生成图片|图片链接|已生成.*图|\[K(?:调用工具|已生成)\]|✨|⏱️|⚙️|💡|🔍/i;

/** 上方已展示媒体时，下方多为 MCP 工具复述，可省略正文与工具条 */
export function shouldHideAssistantTextWhenMediaShown(
  content: string,
  media: ChatMediaItem[],
): boolean {
  if (!media.length) return false;
  const body = stripMediaFromAssistantContent(content).trim();
  if (!body) return true;
  return MCP_MEDIA_REPLY_RE.test(body);
}
