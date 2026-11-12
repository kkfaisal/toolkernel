"""An MCP server for the web: search (Tavily or Brave) and page fetching.

    TAVILY_API_KEY=... python -m toolkernel_gateway.mcp_servers.web     # or BRAVE_API_KEY=...

web_search uses Tavily (https://tavily.com) when TAVILY_API_KEY is set, otherwise the Brave Search API
(https://brave.com/search/api/). Both have free tiers.
fetch_url downloads a public web page and returns its readable text. Private, loopback and
link-local addresses are refused, so the tool can't be used to reach your internal network.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import re
import socket
import ssl
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # the only exception whose message reaches the caller
from mcp_types import ToolAnnotations

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_RANGE = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}
MAX_BYTES = 5_000_000
UA = "Mozilla/5.0 (compatible; ToolKernel-web/0.1; +https://github.com/)"
RO_OPEN = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _tls() -> ssl.SSLContext | bool:
    """Trust what the operating system trusts (works behind corporate TLS inspection), else certifi."""
    if os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


TLS = _tls()

mcp = MCPServer(
    "web",
    instructions="Search the web with web_search, then read pages with fetch_url. "
                 "Pages come back as plain text and can be long.",
)


@contextlib.contextmanager
def _http_errors(what: str):
    """Report network and HTTP failures to the caller in words (the SDK hides other exceptions' messages)."""
    try:
        yield
    except httpx.HTTPStatusError as e:
        raise ToolError(f"{what}: HTTP {e.response.status_code} from {e.request.url}") from e
    except httpx.HTTPError as e:
        raise ToolError(f"{what}: {type(e).__name__}: {e}") from e


@mcp.tool(annotations=RO_OPEN)
def web_search(query: str, count: int = 10, offset: int = 0, freshness: str | None = None,
               country: str | None = None) -> list[dict[str, Any]]:
    """Search the web. Returns results with title, url, description and age.

    count: results per page (max 20). offset: page number, 0-based (max 9; Brave only).
    freshness: pd (past day), pw (past week), pm (past month), py (past year), or "YYYY-MM-DDtoYYYY-MM-DD" (Brave only).
    country: 2-letter country code, e.g. "us" (Brave only).
    """
    if os.environ.get("TAVILY_API_KEY"):
        return _tavily(query, count, freshness)
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        raise ToolError("No search key is set. Set TAVILY_API_KEY (https://tavily.com) or BRAVE_API_KEY "
                        "(https://brave.com/search/api/) in the environment, or as ${secret:...} in mcp.json.")
    params: dict[str, Any] = {"q": query, "count": max(1, min(count, 20)), "offset": max(0, min(offset, 9))}
    if freshness:
        params["freshness"] = freshness
    if country:
        params["country"] = country
    with _http_errors("web search failed"):
        r = httpx.get(BRAVE_URL, params=params, timeout=20, verify=TLS,
                      headers={"Accept": "application/json", "X-Subscription-Token": key})
        if r.status_code == 429:
            raise ToolError("Brave Search rate limit reached; wait a moment and retry")
        r.raise_for_status()
    data = r.json()
    out = []
    for item in (data.get("web") or {}).get("results", []):
        out.append({
            "title": item.get("title"),
            "url": item.get("url"),
            "description": _strip_tags(item.get("description") or ""),
            "age": item.get("age") or item.get("page_age"),
            "extra_snippets": [_strip_tags(s) for s in item.get("extra_snippets", [])][:3],
        })
    return out


def _tavily(query: str, count: int, freshness: str | None) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"query": query, "max_results": max(1, min(count, 20)), "search_depth": "basic"}
    if freshness in TAVILY_RANGE:
        body["time_range"] = TAVILY_RANGE[freshness]
    with _http_errors("web search failed"):
        r = httpx.post(TAVILY_URL, json=body, timeout=30, verify=TLS,
                       headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"})
        if r.status_code == 429:
            raise ToolError("Tavily rate limit reached; wait a moment and retry")
        r.raise_for_status()
    return [{"title": it.get("title"), "url": it.get("url"), "description": it.get("content") or "",
             "age": it.get("published_date")} for it in r.json().get("results", [])]


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n\n", t)
        return t.strip()


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


def _check_public(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ToolError("only http(s) URLs can be fetched")
    for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ToolError(f"{u.hostname} resolves to a non-public address; refusing to fetch it")


@mcp.tool(annotations=RO_OPEN)
def fetch_url(url: str, max_chars: int | None = None) -> dict[str, Any]:
    """Fetch a public web page and return its readable text (scripts, styles and navigation removed).

    max_chars: cut the text after this many characters. By default the whole page is returned.
    """
    current = url
    with _http_errors(f"could not fetch {url}"), \
            httpx.Client(timeout=25, headers={"User-Agent": UA}, follow_redirects=False, verify=TLS) as client:
        for _ in range(6):  # follow redirects ourselves so every hop is checked
            _check_public(current)
            with client.stream("GET", current) as r:
                if r.is_redirect and r.headers.get("location"):
                    current = str(r.url.join(r.headers["location"]))
                    continue
                r.raise_for_status()
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        break
                ctype = r.headers.get("content-type", "")
                charset = r.charset_encoding or "utf-8"
                break
        else:
            raise ToolError("too many redirects")
    raw = body.decode(charset, errors="replace")
    if "html" in ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        p = _TextExtractor()
        p.feed(raw)
        text, title = p.text(), " ".join(p.title.split())
    else:
        text, title = raw, ""
    full = len(text)
    if max_chars is not None:
        text = text[: max(0, max_chars)]
    return {"url": current, "title": title, "content_type": ctype.split(";")[0], "chars": full, "text": text}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
