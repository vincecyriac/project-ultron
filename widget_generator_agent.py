"""
Widget HTML generator — the second stage of the async card pipeline.

Gemini Live never composes a card itself; it names what is wanted and returns to
the conversation. This module turns that name into finished markup on a fast
model, so the voice is never blocked on layout work.

Everything it produces is sanitised before it reaches the browser (see
sanitize_widget_html). The model is told not to emit scripts, but "told not to"
is not a security boundary — the sanitiser is.
"""

import asyncio
import re

from google.genai import types

WIDGET_MODEL = "gemini-3.7-flash"
GENERATION_TIMEOUT_S = 45.0
MAX_HTML_CHARS = 24000

WIDGET_DESIGN_SYSTEM_PROMPT = """You are FRIDAY's visual interface generator. You output pure, valid, raw HTML for one ambient sci-fi HUD card. No markdown fences, no <script>, no external stylesheets or fonts.

OUTPUT
Return ONLY the inner HTML that goes inside the card body. No <html>, <head>, <body>, and no card chrome — the title bar already exists.

DENSITY
Zero dead space. Every card is packed with structured figures, inline SVG charts, status pills and short briefs. A card with one line in it is a failure. Three to six blocks is normal.

USE THESE CLASSES — they are already styled; do not invent your own colours or write <style> blocks:
  .hud-hero-stat      large monospace figure with a glowing accent
  .hud-hero-row       wraps a hero stat with its label and badge on one line
  .hud-sub            small dim caption under a hero figure
  .hud-badge-green / .hud-badge-red / .hud-badge-cyan / .hud-badge-amber
                      small pills for deltas and status
  .hud-metric-grid    3-column key/value matrix; each cell is
                      <div class="hud-metric"><span class="k">Label</span><span class="v">Value</span></div>
  .hud-feed           wrapper for a list of rows
  .hud-feed-row       one row: <span class="n">01</span><div><span class="tag">CATEGORY</span>
                      <b>Headline</b><p>One-line brief.</p></div>
  .hud-svg-chart      wrapper around an inline <svg>
  .hud-note           a closing one or two line summary
  .hud-bar            <div class="hud-bar"><span style="width:64%"></span></div> for a meter

CHARTS — ONLY WHEN THE DATA EARNS ONE
Most cards do not need a chart. Draw one ONLY when you genuinely hold either
a series of values over time (a price through the day, a week of readings) or
a set of comparable magnitudes worth seeing side by side. If you are working
from a handful of unrelated facts, a status, a definition, a list or a single
figure, there is nothing to plot — a card with no chart is completely normal
and far better than an invented curve. Never draw a chart to fill space, and
never fabricate the points behind one.

When it IS warranted:
<div class="hud-svg-chart"><svg viewBox="0 0 400 120" preserveAspectRatio="none">
  <defs><linearGradient id="gUNIQUE" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="currentColor" stop-opacity="0.30"/>
    <stop offset="100%" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>
  <path d="M0 90 L50 72 ... L400 30 L400 120 L0 120 Z" fill="url(#gUNIQUE)"/>
  <line class="ref" x1="0" y1="70" x2="400" y2="70"/>
  <path class="line" d="M0 90 L50 72 ... L400 30"/>
</svg></div>
Give every gradient id a unique suffix. Put the class "up" or "down" on the wrapper to colour it.

IMAGERY
When the subject is news, a place, a product, a person or anything else that
reads better with a picture, include ONE real image you found while searching:
  <img src="https://real.url/from/the/article.jpg" alt="short description">
Use a URL you actually saw in a search result — the article's own lead photo is
ideal. Do not reconstruct a plausible-looking CDN path from memory. One image
per card at most. Unreachable images are removed automatically before display,
so include one whenever the subject genuinely reads better with a picture.

HONESTY
Use figures from the context you were given. Where you do not have a real number, omit that cell rather than inventing one — a made-up price is worse than a missing one. If the context is thin, say so in a .hud-note instead of padding the card with fiction."""


def sanitize_widget_html(html: str) -> str:
    """Strip anything executable before this markup reaches the DOM.

    The model is instructed not to emit scripts, but instructions are not a
    boundary — context reaching the generator can come from web pages the user
    asked about, so treat the output as untrusted.
    """
    if not html:
        return ""

    # Whole elements that can execute or phone out.
    for tag in ("script", "iframe", "object", "embed", "link", "meta", "base", "form"):
        html = re.sub(rf"<\s*{tag}\b[^>]*>.*?<\s*/\s*{tag}\s*>", "", html,
                      flags=re.I | re.S)
        html = re.sub(rf"<\s*{tag}\b[^>]*/?>", "", html, flags=re.I)

    # Inline handlers: onclick=, onerror=, onload= …
    html = re.sub(r"\son[a-z]+\s*=\s*\"[^\"]*\"", "", html, flags=re.I)
    html = re.sub(r"\son[a-z]+\s*=\s*'[^']*'", "", html, flags=re.I)
    html = re.sub(r"\son[a-z]+\s*=\s*[^\s>]+", "", html, flags=re.I)

    # javascript: / data: URLs in href and src
    html = re.sub(r"(href|src|xlink:href)\s*=\s*([\"'])\s*(javascript|data|vbscript):[^\"']*\2",
                  r"\1=\2#\2", html, flags=re.I)

    # A <style> block would leak out of the card and restyle the whole GUI.
    html = re.sub(r"<\s*style\b[^>]*>.*?<\s*/\s*style\s*>", "", html, flags=re.I | re.S)

    return html.strip()[:MAX_HTML_CHARS]


def _strip_fences(text: str) -> str:
    """Models still wrap output in ```html fences despite being told not to."""
    out = (text or "").strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\s*", "", out)
        if out.endswith("```"):
            out = out[:-3]
    return out.strip()


IMG_SRC_RE = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\'][^>]*>', re.I)


async def verify_images(html: str) -> str:
    """Verify each <img>, then route it through the hub's image proxy.

    Two things go wrong with model-supplied image URLs, both observed: a
    reconstructed CDN path that 401s, and a real article image that 403s on a
    direct browser request because of hotlink protection. So the hub fetches
    each one itself — with a browser UA and the image's own origin as Referer —
    and the surviving ones are rewritten to load from localhost. Anything that
    cannot be fetched is removed rather than left as a broken frame.
    """
    urls = set(IMG_SRC_RE.findall(html))
    if not urls:
        return html

    try:
        import aiohttp
    except Exception:
        return html

    from urllib.parse import urlsplit, quote

    async def ok(session, url):
        if not url.lower().startswith(("http://", "https://")):
            return url, False
        origin = "{0.scheme}://{0.netloc}/".format(urlsplit(url))
        try:
            async with session.get(url, headers={"Referer": origin}) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                return url, r.status < 400 and ctype.startswith("image/")
        except Exception:
            return url, False

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}
        async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                                         headers=headers) as session:
            results = dict(await asyncio.gather(*(ok(session, u) for u in urls)))
    except Exception:
        return html

    def rewrite(match):
        url = match.group(1)
        if not results.get(url):
            return ""                                  # unreachable: drop the frame
        return match.group(0).replace(url, "/img?u=" + quote(url, safe=""))

    return IMG_SRC_RE.sub(rewrite, html)


async def generate_widget_html(client, title: str, query_context: str,
                               use_search: bool = True) -> str:
    """Compose one card's inner HTML. Returns "" if nothing usable came back."""
    prompt = (f"Widget title: {title}\n"
              f"What the card must show: {query_context}\n\n"
              "Generate the high-density inner HTML now.")

    config = types.GenerateContentConfig(
        system_instruction=WIDGET_DESIGN_SYSTEM_PROMPT,
        temperature=0.2,
        # Reasoning off: this is layout work on a fast model, and the tail
        # latency matters more here than marginal quality.
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    if use_search:
        # Real figures beat plausible ones; the card is worthless if invented.
        config.tools = [types.Tool(google_search=types.GoogleSearch())]

    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=WIDGET_MODEL, contents=prompt, config=config),
        timeout=GENERATION_TIMEOUT_S,
    )
    html = sanitize_widget_html(_strip_fences(response.text))
    return await verify_images(html)
