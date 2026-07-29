"""
sentry_web.py - Web page reader for Project Ultron.

The Gemini Live session already has the google_search grounding tool for
queries; this module complements it by fetching a specific URL and
converting the page to readable plain text so the model can quote and
reason over full articles, docs, and dashboards.
"""

import re
import ssl
import aiohttp

MAX_TEXT_CHARS = 9000

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _html_to_text(html: str) -> str:
    # Drop non-content blocks entirely
    html = re.sub(r"(?is)<(script|style|noscript|svg|head|iframe)[^>]*>.*?</\1>", " ", html)
    # Preserve some structure
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article)>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "- ", html)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common entities
    for ent, ch in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                    ("&#39;", "'"), ("&nbsp;", " "), ("&mdash;", "—"), ("&ndash;", "–")]:
        text = text.replace(ent, ch)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


async def fetch_webpage(url: str) -> str:
    """Fetches a URL and returns its readable text content (truncated)."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return "[Error]: Invalid URL. Must start with http:// or https://."
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=_HEADERS) as session:
            async with session.get(url, ssl=_ssl_ctx, allow_redirects=True) as resp:
                if resp.status >= 400:
                    return f"[Error]: HTTP {resp.status} fetching {url}."
                ctype = resp.headers.get("Content-Type", "")
                raw = await resp.text(errors="replace")
                if "html" in ctype or raw.lstrip()[:1] == "<":
                    text = _html_to_text(raw)
                else:
                    text = raw
                if len(text) > MAX_TEXT_CHARS:
                    text = text[:MAX_TEXT_CHARS] + "\n...[content truncated]"
                title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
                title = title_match.group(1).strip() if title_match else url
                return f"Page: {title}\nURL: {url}\n\n{text}"
    except Exception as e:
        return f"[Error]: Failed to fetch {url}: {e}"
