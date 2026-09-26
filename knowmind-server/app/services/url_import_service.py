from __future__ import annotations

import html as html_lib
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

from app.utils.outbound_security import validate_public_http_url

log = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

_BLOCK_TAGS = frozenset({"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "article", "section"})

# 仅匹配页脚 / 法律声明 / 备案等无用行（不删正文、不压缩篇幅）
_FOOTER_NOISE_RES = [
    re.compile(p, re.I)
    for p in (
        r"copyright\s*©|©\s*\d{4}",
        r"all\s+rights\s+reserved",
        r"版权所有|保留所有权利",
        r"备案号\s*[：:]\s*[\w\d\-]+",
        r"ICP备\s*\d+号?(-\d+)?",
        r"ICP证\s*\d+",
        r"公安备案",
        r"公网安备\s*\d+",
        r"privacy\s+policy|terms\s+of\s+use|cookie\s+policy",
        r"本站.*?仅供学习使用",
        r"runoob\.com.*(copyright|reserved|备案|rights)",
        r"菜鸟教程.*(copyright|reserved|备案|rights)",
    )
]


class _TextExtractor(HTMLParser):
    """HTML 降级抽取：仅跳过 script/style 与语义化页眉页脚。"""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    @property
    def _skip(self) -> bool:
        return self._skip_depth > 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        t = data.strip()
        if t:
            self._chunks.append(t + " ")

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        raw = re.sub(r"[ \t]{2,}", " ", raw)
        return raw.strip()


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL 不能为空")
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url.lstrip('/')}"
        parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("仅支持 http/https URL，请检查格式（可省略 https://，会自动补全）")
    return url


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    if m:
        return html_lib.unescape(re.sub(r"\s+", " ", m.group(1)).strip())[:200]
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if m:
        return html_lib.unescape(m.group(1).strip())[:200]
    return None


def _meta_content(html: str, *, name: str | None = None, prop: str | None = None) -> str | None:
    if name:
        patterns = [
            rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(name)}["\']',
        ]
    elif prop:
        patterns = [
            rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']+)["\']',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(prop)}["\']',
        ]
    else:
        return None
    for pat in patterns:
        m = re.search(pat, html, re.I | re.S)
        if m:
            text = html_lib.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
            if text:
                return text
    return None


def _extract_fallback_text(html: str) -> str:
    parts: list[str] = []
    for key in ("description", "og:description", "twitter:description"):
        if key.startswith("og:") or key.startswith("twitter:"):
            val = _meta_content(html, prop=key)
        else:
            val = _meta_content(html, name=key)
        if val:
            parts.append(val)
    return "\n".join(dict.fromkeys(parts))


def _is_footer_noise_line(line: str) -> bool:
    """只删页脚/备案/Copyright 等，不删教程正文。"""
    s = line.strip()
    if not s:
        return False
    for pat in _FOOTER_NOISE_RES:
        if pat.search(s):
            return True
    return False


def clean_scraped_text(text: str) -> str:
    """轻量清洗：去掉页脚噪声行，保留与原文相近的篇幅与段落结构。"""
    if not text:
        return ""
    lines = text.splitlines()
    kept: list[str] = []
    for raw in lines:
        if _is_footer_noise_line(raw):
            continue
        kept.append(raw.rstrip())
    body = "\n".join(kept)
    body = re.sub(r"\n{4,}", "\n\n\n", body)
    return body.strip()


def _plain_to_markdown(text: str) -> str:
    """纯文本降级为简单 Markdown（保留段落，不缩减内容）。"""
    if not text.strip():
        return ""
    if re.search(r"^#{1,6}\s|\*\*|```|^-\s", text, re.M):
        return text.strip()
    blocks = re.split(r"\n\s*\n", text.strip())
    parts: list[str] = []
    for block in blocks:
        b = block.strip()
        if not b:
            continue
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) == 1 and len(lines[0]) <= 40 and not lines[0].endswith(("。", ".", "!", "?")):
            parts.append(f"## {lines[0]}")
        elif all(re.match(r"^[-*•]\s+", ln) or len(ln) < 80 for ln in lines) and len(lines) > 1:
            bullet_re = re.compile(r"^[-*•]\s+")
            parts.append("\n".join(f"- {bullet_re.sub('', ln)}" for ln in lines))
        else:
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _extract_with_trafilatura(html: str, url: str) -> str:
    try:
        # 勿设 include_comments=False：菜鸟教程等站点正文常被误当作 comment 丢弃
        kwargs = {
            "url": url,
            "include_tables": True,
            "favor_recall": True,
            "deduplicate": False,
        }
        md = trafilatura.extract(html, output_format="markdown", **kwargs)
        if md and md.strip():
            return md.strip()
        text = trafilatura.extract(html, **kwargs)
        return (text or "").strip()
    except Exception as e:
        log.debug("trafilatura 抽取失败: %s", e)
        return ""


def _extract_body_text(html: str, url: str) -> str:
    body = _extract_with_trafilatura(html, url)
    if len(body) < 80:
        parser = _TextExtractor()
        parser.feed(html)
        body = parser.text()
    if len(body) < 80:
        fallback = _extract_fallback_text(html)
        if fallback:
            body = f"{body}\n\n{fallback}".strip() if body else fallback
    body = clean_scraped_text(body)
    if body and not re.search(r"^#{1,6}\s|^\*\*|^```|^-\s", body, re.M):
        body = _plain_to_markdown(body)
    return body


def _guess_title(content: str, page_title: str | None, url: str) -> str:
    if page_title:
        return page_title.strip()[:200]
    for line in content.splitlines():
        m = re.match(r"^#{1,6}\s+(.+)", line.strip())
        if m:
            return m.group(1).strip()[:200]
        if line.strip() and len(line.strip()) <= 80:
            return line.strip()[:200]
    return url[:200]


def _first_paragraph_summary(content: str) -> str | None:
    for block in re.split(r"\n\s*\n", content):
        t = re.sub(r"^#{1,6}\s+", "", block.strip())
        t = re.sub(r"\s+", " ", t)
        if len(t) >= 20:
            return t[:200]
    return None


def build_url_item_fields(url: str, raw_text: str, page_title: str | None) -> dict:
    """保留清洗后全文为 Markdown，不做 AI 缩写。"""
    content = clean_scraped_text(raw_text) or raw_text.strip()
    if content and not re.search(r"^#{1,6}\s|^\*\*|^```|^-\s", content, re.M):
        content = _plain_to_markdown(content)
    title = _guess_title(content, page_title, url)
    summary = _first_paragraph_summary(content)
    return {
        "title": title,
        "summary": summary,
        "content": content[:50000],
    }


async def distill_url_to_item_fields(url: str, raw_text: str, page_title: str | None) -> dict:
    """兼容旧名：URL 采集不再用 LLM 压缩正文。"""
    return build_url_item_fields(url, raw_text, page_title)


async def fetch_url_text(url: str, *, max_bytes: int = 2_000_000) -> tuple[str, str | None]:
    url = _normalize_url(url)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=False,
        headers=_BROWSER_HEADERS,
    ) as client:
        current = url
        raw = b""
        r: httpx.Response | None = None
        for _ in range(6):
            await validate_public_http_url(current)
            try:
                async with client.stream("GET", current) as response:
                    r = response
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("网页重定向缺少目标地址")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("网页体积过大")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    break
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code == 403:
                    raise ValueError("目标网站拒绝访问（403），请换链接或手动复制正文") from e
                if code == 404:
                    raise ValueError("网页不存在（404）") from e
                raise ValueError(f"网页请求失败（HTTP {code}）") from e
            except httpx.RequestError as e:
                raise ValueError("无法访问该 URL，请检查网络或链接是否正确") from e
        else:
            raise ValueError("网页重定向次数过多")
        if r is None:
            raise ValueError("网页请求失败")
        html = raw.decode(r.encoding or "utf-8", errors="replace")
    title = _extract_title(html)
    body = _extract_body_text(html, url)
    if not body:
        raise ValueError("未能从网页提取正文（可能是纯 JavaScript 页面），请换链接或手动录入")
    return body[:50000], title
