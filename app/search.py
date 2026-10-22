"""
Search + fetch. DuckDuckGo (no key) for discovery, httpx + trafilatura for
page text. Results from the provider's own domains are ranked first because
the whole point is to prefer official sources over blog posts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

UA = "Mozilla/5.0 (compatible; oauth-guide-agent/0.1; +https://github.com/)"

# Domains that are usually noise for this task
NOISE = ("youtube.com", "reddit.com", "medium.com", "pinterest.", "facebook.com/groups",
         "quora.com", "linkedin.com/pulse", "udemy.com",
         "enterprise-server@", "enterprise-cloud@")  # self-hosted variants pollute github.com guides

# Hosts where most paths are user-generated content, so "same domain" does not mean "official".
# Only the listed path prefixes (or the bare host) count as official on these hosts.
USER_CONTENT_HOSTS = {
    "gist.github.com": (),
    "raw.githubusercontent.com": (),
    "github.io": (),
    "github.com": ("/settings", "/login", "/apps", "/marketplace", "/features", "/enterprise",
                   "/github/", "/orgs/github/", "/organizations/"),
    "gitlab.com": ("/-/", "/users/", "/admin/", "/oauth/", "/help", "/gitlab-org/"),
    "notion.site": (),
    "notion.so": ("/help", "/my-integrations", "/profile", "/product"),
    "bitbucket.org": ("/account", "/product", "/blog", "/site/"),
}


@dataclass
class Hit:
    url: str
    title: str
    snippet: str
    official: bool = False


@dataclass
class Page:
    url: str
    title: str
    text: str
    official: bool = False
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: int = 0
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, anchor text) found on the page


def host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def root_domain(url_or_host: str) -> str:
    h = host(url_or_host) if "://" in url_or_host else url_or_host.lower().removeprefix("www.")
    parts = h.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return h


def is_official(url: str, official_roots: set[str]) -> bool:
    h = host(url)
    if not any(h == r or h.endswith("." + r) for r in official_roots):
        return False
    for uc_host, allowed in USER_CONTENT_HOSTS.items():
        # exact host match, except github.io / notion.site where every subdomain is a user's
        if h == uc_host or (uc_host in ("github.io", "notion.site") and h.endswith("." + uc_host)):
            path = urlparse(url).path or "/"
            return path == "/" or any(path.startswith(pfx) for pfx in allowed)
    return True


_NOISE_RE = re.compile(r"github\.com/[^/]+/[^/]+/(blob|tree|commits?|issues|pull)/|gitlab\.com/.+/-/(blob|tree)/")


def is_noise(url: str) -> bool:
    return any(n in url for n in NOISE) or bool(_NOISE_RE.search(url))


def web_search(query: str, max_results: int = 8) -> list[Hit]:
    """Tavily if TAVILY_API_KEY is set (more reliable on cloud hosts), else DuckDuckGo."""
    import os
    key = os.getenv("TAVILY_API_KEY", "")
    if key:
        try:
            r = httpx.post("https://api.tavily.com/search", timeout=20,
                           json={"api_key": key, "query": query, "max_results": max_results,
                                 "search_depth": "basic"})
            if r.status_code == 200:
                hits = []
                for x in r.json().get("results", []):
                    url = x.get("url", "")
                    if url and not is_noise(url):
                        hits.append(Hit(url=url, title=x.get("title", ""), snippet=(x.get("content") or "")[:300]))
                return hits
        except Exception:
            pass
    return ddg_search(query, max_results)


def ddg_search(query: str, max_results: int = 8) -> list[Hit]:
    try:
        from ddgs import DDGS
        with DDGS() as d:
            rows = d.text(query, max_results=max_results) or []
    except Exception:
        return []
    hits: list[Hit] = []
    for r in rows:
        url = r.get("href") or r.get("url") or ""
        if not url or is_noise(url):
            continue
        hits.append(Hit(url=url, title=r.get("title", ""), snippet=r.get("body", "")))
    return hits


def fetch(url: str, timeout: float = 15.0, max_chars: int = 12000, reader_fallback: bool = False) -> Page | None:
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as c:
            r = c.get(url)
    except Exception:
        return None
    if r.status_code >= 400:
        return _reader(url, max_chars, r.status_code) if reader_fallback else Page(url=url, title="", text="", status=r.status_code)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return Page(url=str(r.url), title="", text="", status=r.status_code)
    html = r.text
    page = _parse(str(r.url), html, r.status_code, max_chars)
    if len(page.text) < 300 and reader_fallback:
        # JS-rendered docs (developer.intuit.com, many Salesforce/Oracle portals) have no text in
        # the raw HTML; a rendering reader service gets what a browser would see
        alt = _reader(url, max_chars, r.status_code)
        if alt and len(alt.text) > len(page.text):
            alt.links = alt.links or page.links
            return alt
    return page


def _reader(url: str, max_chars: int, orig_status: int) -> Page | None:
    """Fetch through r.jina.ai (free, keyless, renders JavaScript). Off when READER_FALLBACK=0."""
    import os
    if os.getenv("READER_FALLBACK", "1") == "0":
        return Page(url=url, title="", text="", status=orig_status)
    try:
        with httpx.Client(follow_redirects=True, timeout=40.0,
                          headers={"User-Agent": UA, "X-Return-Format": "markdown"}) as c:
            r = c.get("https://r.jina.ai/" + url)
    except Exception:
        return Page(url=url, title="", text="", status=orig_status)
    if r.status_code != 200 or len(r.text) < 300:
        return Page(url=url, title="", text="", status=orig_status)
    text = r.text
    title = ""
    m = re.search(r"^Title:\s*(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()[:200]
    m = re.search(r"^Markdown Content:\s*$", text, re.M)
    if m:
        text = text[m.end():]
    links = [(urljoin(url, href), anchor[:120]) for anchor, href in re.findall(r"\[([^\]]{1,120})\]\((https?://[^)\s]+|/[^)\s]*)\)", text)]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    return Page(url=url, title=title, text=text, status=200, links=links[:400])


def _parse(url: str, html: str, status: int, max_chars: int) -> Page:
    text = trafilatura.extract(html, include_links=False, include_tables=True, favor_recall=True) or ""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    # compact whitespace, cap length
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    links = []
    for href, anchor in re.findall(r'<a\s[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', html, re.I | re.S)[:400]:
        anchor = re.sub(r"<[^>]+>|\s+", " ", anchor).strip()
        if href.startswith(("http://", "https://", "/")):
            links.append((urljoin(url, href), anchor[:120]))
    return Page(url=url, title=title, text=text, status=status, links=links)


def probe(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """Hit a URL and return (status, final_url). Used to test whether an
    endpoint or portal actually exists, not to read it."""
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": UA}) as c:
            r = c.get(url)
            return r.status_code, str(r.url)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:120]


def well_known(domain: str) -> dict:
    """Try OIDC / OAuth AS discovery documents on a domain. Finding one is
    strong evidence that OAuth exists and gives the real endpoints."""
    found: dict = {}
    for path in ("/.well-known/openid-configuration", "/.well-known/oauth-authorization-server"):
        for scheme_host in (f"https://{domain}", f"https://accounts.{domain}", f"https://auth.{domain}",
                            f"https://api.{domain}", f"https://login.{domain}"):
            url = scheme_host + path
            try:
                with httpx.Client(follow_redirects=True, timeout=8.0, headers={"User-Agent": UA}) as c:
                    r = c.get(url)
                if r.status_code == 200 and "authorization_endpoint" in r.text:
                    data = r.json()
                    found = {
                        "url": url,
                        "authorization_endpoint": data.get("authorization_endpoint"),
                        "token_endpoint": data.get("token_endpoint"),
                        "code_challenge_methods_supported": data.get("code_challenge_methods_supported"),
                        "scopes_supported": (data.get("scopes_supported") or [])[:30],
                    }
                    return found
            except Exception:
                continue
    return found
