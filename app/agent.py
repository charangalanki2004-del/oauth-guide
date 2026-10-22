"""
The investigating agent.

Pipeline (each stage emits a progress event so the UI can show it live):

  1. identify   what is this app, what are its official domains, where is the dev portal
  2. search     five targeted queries; official domains ranked first
  3. fetch      read the top pages (capped by MAX_PAGES)
  4. probe      things the agent can actually test: OIDC discovery, portal reachability,
                Composio managed-auth lookup
  5. extract    LLM fills the Guide schema from the fetched text only
  6. validate   drop citations to URLs we never fetched, downgrade their evidence,
                compute difficulty + confidence, merge any human-tested notes

Anti-hallucination rules live in `validate`: the model can only cite URLs we
fetched, and any claim without a fetched official URL is downgraded to
"inferred". The difficulty score is computed, never asked for.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import llm, rubric, search
from .schema import (AgentAttempt, AppType, ApprovalGate, Blocker, Claim, Evidence, Guide, RegistrationAccess,
                     Source, Step, TimeRange, WaitRange, slugify)

Progress = Callable[[str, str, Optional[dict]], None]

DATA = Path(__file__).resolve().parent.parent / "data"

QUERY_TEMPLATES = [
    "{name} OAuth app developer documentation",
    "{name} create OAuth app client id client secret redirect URI",
    "{name} OAuth app review approval verification requirements",
    "{name} OAuth scopes permissions",
    "{name} developer app publish production limits unverified",
]

IDENTIFY_SYSTEM = """You identify software products for a developer tool. Reply with JSON only."""

IDENTIFY_USER = """A developer typed: "{query}"

Search results:
{results}

Return JSON:
{{
  "name": "canonical product name",
  "website": "https://homepage",
  "official_domains": ["example.com", "developer.example.com"],
  "developer_portal_url": "https://... where a developer would create an OAuth app, or null",
  "category": "one of: productivity, crm, communication, developer, finance, commerce, marketing, hr, storage, social, design, support, analytics, other",
  "likely_has_oauth": true/false/null
}}
Only include domains you are confident belong to the product's owner."""

EXTRACT_SYSTEM = """You are an expert on OAuth provider onboarding. You turn provider documentation into a
structured setup guide for developers who want to register their own OAuth app so their users can
connect accounts to an AI agent.

Rules you must follow:
- Use ONLY the source pages provided. Do not use outside knowledge for factual claims.
- Every claim has an "evidence" tier:
    "sourced"  -> the text is supported by one of the provided pages; set source_url to that page's URL
    "inferred" -> your best reading, not directly stated; source_url null
    "unknown"  -> you looked and the sources do not say
- source_url must be one of the provided URLs exactly. Never invent a URL.
- Prefer official provider pages over third-party pages when they disagree.
- Be concrete and short. Developers will skim this. No marketing language.
- If the app does not support OAuth at all, say so (oauth_supported=false) and keep the rest minimal.
- Never invent review, verification, approval, or HTTPS/redirect rules the sources do not state. If no
  review process appears in the sources, say "no review process is documented" rather than describing one.
- Reply with a single JSON object and nothing else."""

EXTRACT_USER = """App: {name}
Website: {website}
Developer portal (if known): {portal}
Agent probes (things we actually tested just now):
{probes}

SOURCE PAGES (URL, then text):
{sources}

Fill this JSON. Enum values are fixed; use them exactly.

{{
  "summary": "2 sentences: the verdict. How hard, what the main gate is, what to do first.",
  "oauth_supported": true | false | null,
  "auth_methods": ["oauth2", "api_key", "basic", "jwt", "..."],
  "oauth_note": {{"text": "which OAuth flows/grant types are offered; or why there is no OAuth", "evidence": "sourced|inferred|unknown", "source_url": null}},
  "developer_portal_url": "https://... or null",
  "app_types": [
    {{"name": "e.g. OAuth App vs GitHub App", "when_to_use": "...", "recommended_for_agents": true}}
  ],
  "registration_access": "anyone | workspace_admin | paid_tier | partner_program | unknown",
  "registration_access_claim": {{"text": "who can create the app and under what account", "evidence": "...", "source_url": null}},
  "prerequisites": [
    {{"text": "account/plan/org/URL needed before you start", "evidence": "...", "source_url": null}}
  ],
  "steps": [
    {{"title": "short imperative", "detail": "exactly what to click/fill, which values matter", "actor": "agent | human | either", "evidence": "...", "source_url": null}}
  ],
  "approval_gate": "none | soft_cap | marketplace_only | review_for_scopes | review_required | partner_only | unknown",
  "approval_claim": {{"text": "what review/verification exists and when it is triggered", "evidence": "...", "source_url": null}},
  "unreviewed_limits": {{"text": "what works before approval (user caps, test users, warning screens)", "evidence": "...", "source_url": null}},
  "review_artifacts": [
    {{"text": "privacy policy URL / demo video / scope justification / security assessment / business verification ...", "evidence": "...", "source_url": null}}
  ],
  "approval_wait": {{"min_days": null, "max_days": null, "evidence": "...", "source_url": null, "note": "what the docs say about review time; say if they say nothing"}},
  "setup_time": {{"min_minutes": 15, "max_minutes": 60, "evidence": "inferred", "source_url": null, "note": "hands-on time to register and get credentials, excluding any review wait"}},
  "redirect_uri_rules": {{"text": "https only? exact match? localhost allowed? how many?", "evidence": "...", "source_url": null}},
  "token_lifecycle": {{"text": "access/refresh token lifetimes, rotation, offline access, traps", "evidence": "...", "source_url": null}},
  "scopes_note": {{"text": "scope naming, sensitive/restricted scopes, incremental auth", "evidence": "...", "source_url": null}},
  "instance_specific": {{"text": "tenant/subdomain/region/data-center specifics of the HOSTED service (e.g. per-shop domains, cloud IDs, EU/US data centers). Do NOT describe self-hosted or enterprise-server editions here. Empty string if none.", "evidence": "...", "source_url": null}},
  "pkce_required": {{"text": "required / supported / not mentioned", "evidence": "...", "source_url": null}},
  "blockers": [
    {{"blocker": "common reason setup or approval stalls", "fix": "what to do about it", "evidence": "...", "source_url": null}}
  ],
  "agent_can": ["things an AI agent can do end to end for this provider (draft fields, build authorize URL, generate manifest, create Composio auth config, ...)"],
  "human_must": ["things only a human can do (log in, own the org, pay, record a demo, sign terms, business verification, ...)"],
  "unverified_claims": ["anything important you could not confirm from these sources"]
}}

Aim for 5-10 steps, 2-5 blockers. Steps must reflect this provider's actual portal, not a generic OAuth tutorial."""


def _hits_block(hits: list[search.Hit]) -> str:
    return "\n".join(f"- {h.title} | {h.url}\n  {h.snippet[:200]}" for h in hits[:10]) or "(none)"


def _identify(query: str, progress: Progress) -> dict:
    is_url = bool(re.match(r"^https?://", query.strip())) or ("." in query and " " not in query)
    seed = query.strip()
    if is_url and not seed.startswith("http"):
        seed = "https://" + seed
    hits = search.web_search(f"{query} developer OAuth", max_results=8)
    if is_url:
        hits = [search.Hit(url=seed, title=seed, snippet="user-supplied website")] + hits
    progress("identify", f"Looking up “{query}”", {"hits": len(hits)})
    if llm.settings.mock:
        return {"name": query.title(), "website": seed if is_url else None,
                "official_domains": [search.root_domain(seed)] if is_url else [],
                "developer_portal_url": None, "category": "other", "likely_has_oauth": None}
    try:
        info = llm.chat_json(IDENTIFY_SYSTEM, IDENTIFY_USER.format(query=query, results=_hits_block(hits)),
                             max_tokens=1500)
    except Exception as e:  # noqa: BLE001
        progress("identify", f"Model call failed during identify: {e}", None)
        info = {}
    info.setdefault("name", query)
    if is_url and not info.get("website"):
        info["website"] = seed
    doms = set(d.lower().removeprefix("www.") for d in info.get("official_domains") or [] if d)
    if info.get("website"):
        doms.add(search.root_domain(info["website"]))
    info["official_roots"] = {search.root_domain(d) for d in doms if d}
    return info


def _search(name: str, official_roots: set[str], progress: Progress, hints: list[str] | None = None,
            portal: str | None = None) -> list[search.Hit]:
    seen: set[str] = set()
    hits: list[search.Hit] = []
    queries = [t.format(name=name) for t in QUERY_TEMPLATES]
    for r in sorted(official_roots)[:1]:
        queries.insert(1, f"OAuth app client id redirect site:{r}")
    # the developer portal's host is a strong signal (console.cloud.google.com, developer.intuit.com):
    # a product name alone pulls in marketplace/listing docs for suite products
    if portal:
        queries.insert(1, f"{search.host(portal)} create OAuth client app")
    for hnt in hints or []:  # catalog entries may carry a search hint for products whose name misleads search
        queries.insert(0, hnt)
    for q in queries:
        for h in search.web_search(q, max_results=6):
            key = h.url.split("#")[0].rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            h.official = search.is_official(h.url, official_roots)
            hits.append(h)
        progress("search", f"Searched: {q}", {"found": len(hits)})
    # official first; within official, pages about creating/registering an app before end-user
    # pages (reviewing/approving/requesting access); then discovery order
    def rank(h: search.Hit) -> tuple:
        path = (h.url + " " + h.title).lower()
        score = 0
        if re.search(r"creat|regist|new-app|setup|set-up|getting-started|quickstart|build", path):
            score -= 2
        if re.search(r"authoriz|scope|token|redirect|callback", path):
            score -= 1
        if re.search(r"review|approv|request|revok|manag|troubleshoot|changelog|blog|community|forum", path):
            score += 2
        return (0 if h.official else 1, score)

    hits.sort(key=rank)
    return hits


SETUP_VERB = re.compile(r"creat|regist|new[- ]app|set[- ]?up|getting[- ]started|quickstart|credential", re.I)
SETUP_NOUN = re.compile(r"\bapp|oauth|client|credential|integration|api[- ]key|consent", re.I)


def _setup_link(anchor: str, slug: str) -> bool:
    """'Creating an OAuth app' yes; 'Create a store listing' / 'Create a promotional badge' no."""
    t = f"{anchor} {slug.replace('-', ' ')}"
    return bool(SETUP_VERB.search(t) and SETUP_NOUN.search(t))


PICK_SYSTEM = """You choose which web pages a developer should read to learn how to register an OAuth app with a
provider and get it approved. Reply with JSON only."""

PICK_USER = """Provider: {name}
We can read only {n} pages. Pick the {n} most useful for: creating/registering the OAuth app in the provider's
developer portal, the approval/verification/review process and its requirements, redirect URI and token rules.
Prefer the provider's own docs. Avoid pages about a different product of the same company, marketplace
listings, SDK references, end-user help, and blog posts, unless nothing better exists.

Candidates:
{hits}

Return JSON: {{"picks": [<candidate numbers in priority order>]}}"""


def _pick(name: str, hits: list[search.Hit], n: int, progress: Progress) -> list[search.Hit]:
    """Let the model choose the reading list from titles/snippets; keyword rank is the fallback."""
    if llm.settings.mock or len(hits) <= n:
        return hits
    cands = hits[:25]
    block = "\n".join(f"{i}. [{'official' if h.official else 'third-party'}] {h.title[:90]} | {h.url}\n   {h.snippet[:140]}"
                      for i, h in enumerate(cands, 1))
    try:
        out = llm.chat_json(PICK_SYSTEM, PICK_USER.format(name=name, n=n + 2, hits=block), max_tokens=1200)
        picks = [int(i) for i in out.get("picks", []) if str(i).isdigit() and 1 <= int(i) <= len(cands)]
    except Exception as e:  # noqa: BLE001
        progress("search", f"Page selection fell back to keyword ranking ({type(e).__name__}: {str(e)[:90]})", None)
        return hits
    if not picks:
        return hits
    chosen = [cands[i - 1] for i in dict.fromkeys(picks)]
    rest = [h for h in hits if h not in chosen]
    progress("search", "Chose a reading list: " + "; ".join((h.title or h.url)[:50] for h in chosen[:n]), None)
    return chosen + rest


def _fetch(hits: list[search.Hit], limit: int, official_roots: set[str], progress: Progress) -> list[search.Page]:
    """Read pages in rank order. When an official page links to a same-site page that
    looks like the "create/register an app" page, queue it next: search engines often
    miss that page but the provider's own navigation never does."""
    pages: list[search.Page] = []
    queue = list(hits)
    seen = {h.url.split("#")[0].rstrip("/") for h in hits}
    followed = 0
    skipped: dict[str, int] = {}
    while queue and len(pages) < limit:
        h = queue.pop(0)
        hst = search.host(h.url)
        if skipped.get(hst, 0) >= 3:
            continue  # this host is not giving us text; stop burning time on it
        p = search.fetch(h.url, reader_fallback=h.official)
        if not p or len(p.text) < 300:
            skipped[hst] = skipped.get(hst, 0) + 1
            why = f"HTTP {p.status}" if p and p.status and p.status != 200 else "no usable text"
            progress("fetch", f"Skipped ({why}): {hst}" + (" — giving up on this host" if skipped[hst] == 3 else ""), None)
            continue
        p.official = h.official
        p.title = p.title or h.title
        pages.append(p)
        progress("fetch", f"Read {'official' if p.official else 'third-party'}: {p.title[:70] or p.url}",
                 {"url": p.url, "official": p.official})
        if not p.official or followed >= 2 or h.snippet.startswith("linked from "):
            continue  # only follow from search hits, never from a followed page; two links per run
        # only sibling pages: same docs section as the page just read (its parent path), so a
        # sidebar link into a different product's docs (e.g. GitHub Apps vs OAuth Apps) is ignored
        section = p.url.split("#")[0].rstrip("/").rsplit("/", 1)[0] + "/"
        found = []
        for url, anchor in p.links:
            key = url.split("#")[0].rstrip("/")
            if key in seen or not key.startswith(section) or "/" in key[len(section):] or search.is_noise(url):
                continue
            if _setup_link(anchor, key.rsplit("/", 1)[-1]):
                seen.add(key)
                found.append(search.Hit(url=url, title=anchor, snippet="linked from " + p.url, official=True))
            if len(found) + followed >= 2:
                break
        if found:
            followed += len(found)
            queue[0:0] = found  # read them next, ahead of lower-ranked search hits
            progress("fetch", f"Following {len(found)} setup link(s) from that page: "
                     + ", ".join(f.title or f.url for f in found), None)
    return pages


def _probe(info: dict, progress: Progress) -> list[AgentAttempt]:
    attempts: list[AgentAttempt] = []
    roots = sorted(info.get("official_roots") or [])
    # 1. OIDC / OAuth AS discovery
    for r in roots[:2]:
        wk = search.well_known(r)
        if wk:
            attempts.append(AgentAttempt(
                action=f"GET {wk['url']}",
                result=f"Discovery document found. authorize={wk.get('authorization_endpoint')} "
                       f"token={wk.get('token_endpoint')} pkce={wk.get('code_challenge_methods_supported')}",
                ok=True))
            progress("probe", f"Found OAuth discovery document on {r}", wk)
            break
    else:
        if roots:
            attempts.append(AgentAttempt(
                action=f"GET /.well-known/openid-configuration on {', '.join(roots[:2])}",
                result="No discovery document. Normal for many providers; endpoints must come from docs.",
                ok=False))
            progress("probe", "No OAuth discovery document (common; not a bad sign)", None)
    # 2. Developer portal reachability
    portal = info.get("developer_portal_url")
    if portal:
        status, final = search.probe(portal)
        ok = 200 <= status < 400
        login = ok and final != portal and re.search(r"login|signin|sign-in|auth|sso", final, re.I)
        if login:
            result = f"HTTP {status}, redirects to a login page ({search.host(final)}). Portal exists; needs a human session."
        else:
            result = f"HTTP {status}" + (f" → {final}" if final != portal else "")
        attempts.append(AgentAttempt(action=f"GET {portal}", result=result, ok=ok))
        progress("probe", f"Developer portal {'exists (behind login)' if login else f'responded HTTP {status}'}",
                 {"url": portal})
    # 3. Composio managed auth lookup
    slug = slugify(info.get("name", ""))
    key = os.getenv("COMPOSIO_API_KEY", "")
    try:
        headers = {"x-api-key": key} if key else {}
        r = httpx.get(f"https://backend.composio.dev/api/v3.1/toolkits/{slug}", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            managed = data.get("composio_managed_auth_schemes") or []
            schemes = data.get("auth_config_details") or data.get("auth_schemes") or []
            info["composio_managed"] = any("OAUTH" in str(m).upper() for m in managed)
            info["composio_note"] = (f"Composio toolkit '{slug}' found. Managed auth schemes: {managed or 'none'}.")
            attempts.append(AgentAttempt(action=f"Composio toolkit lookup for '{slug}'",
                                         result=info["composio_note"], ok=True))
            progress("probe", "Checked Composio managed-auth directory", {"managed": info["composio_managed"]})
        elif r.status_code in (401, 403):
            # No key: Composio publishes a public knowledge page per toolkit that links the
            # toolkit page and, where one exists, its own "create OAuth2 credentials" guide.
            # Composio slugs are usually un-hyphenated ("googledrive"), so try both forms.
            kb, kr, body = "", None, ""
            for cand in dict.fromkeys([slug, slug.replace("-", "")]):
                kb = f"https://docs.composio.dev/kb/toolkit/{cand}"
                kr = httpx.get(kb + ".md", headers={"User-Agent": search.UA}, timeout=10, follow_redirects=True)
                body = kr.text if kr.status_code == 200 else ""
                if f"/toolkits/{cand}" in body:
                    slug = cand
                    break
            if f"/toolkits/{slug}" in body:
                m = re.search(r"\((https://composio\.dev/auth/[^)\s]+)\)", body)
                note = f"Composio has a '{slug}' toolkit ({kb})."
                if m:
                    note += (f" Composio's own OAuth credential guide for it: {m.group(1)}. When registering, use the "
                             "redirect URI shown in your Composio auth config.")
                info["composio_note"] = note
                attempts.append(AgentAttempt(action=f"GET {kb}", result="HTTP 200: Composio toolkit exists"
                                             + (", with an OAuth setup guide." if m else "."), ok=True))
                progress("probe", "Found the app in Composio's toolkit directory", {"url": kb})
            else:
                attempts.append(AgentAttempt(action=f"Composio toolkit lookup for '{slug}'",
                                             result=f"API needs COMPOSIO_API_KEY; public page {kb} returned HTTP {kr.status_code}.",
                                             ok=False if kr.status_code == 404 else None))
        else:
            attempts.append(AgentAttempt(action=f"Composio toolkit lookup for '{slug}'",
                                         result=f"HTTP {r.status_code}; no toolkit with that slug.", ok=False))
    except Exception as e:  # noqa: BLE001
        attempts.append(AgentAttempt(action="Composio toolkit lookup", result=f"failed: {e}", ok=None))
    return attempts


def _sources_block(pages: list[search.Page], budget: int | None = None) -> str:
    """Concatenate page text within a total character budget. Pages are already
    ordered official-first, so each page takes an equal share of what remains and
    short pages donate their surplus to later ones."""
    budget = budget or llm.settings.max_source_chars
    parts = []
    remaining = budget
    n_official = sum(1 for p in pages if p.official)
    for i, p in enumerate(pages, 1):
        # official pages split the budget among themselves; third-party pages get what is left
        left = n_official - (i - 1) if p.official else len(pages) - i + 1
        share = max(remaining // max(left, 1), 300)
        text = p.text if len(p.text) <= share else p.text[:share].rsplit("\n", 1)[0] + "\n[...truncated]"
        remaining = max(remaining - len(text), 0)
        tag = "OFFICIAL" if p.official else "third-party"
        parts.append(f"### Source {i} [{tag}] {p.url}\nTitle: {p.title}\n{text}\n")
    return "\n".join(parts) or "(no pages could be read)"


def _coerce_claim(v) -> Optional[Claim]:
    if not v:
        return None
    if isinstance(v, str):
        return Claim(text=v)
    try:
        return Claim(**{k: v.get(k) for k in ("text", "evidence", "source_url") if v.get(k) is not None})
    except Exception:  # noqa: BLE001
        return None


def _canon(url: str) -> str:
    u = url.strip().lower().split("#")[0]
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = re.sub(r"\?(hl|lang|locale|utm_[a-z]+)=[^&]*(&|$)", "?", u)  # drop locale/tracking params only
    return u.rstrip("?").rstrip("/")


def _coerce_list(items, model, required: tuple[str, ...]):
    """Build `model` from each dict, dropping items the model rejects instead of
    failing the whole guide. Free-tier models occasionally omit a field."""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or not all(str(it.get(k) or "").strip() for k in required):
            continue
        it = {k: v for k, v in it.items() if k in model.model_fields}
        for k, f in model.model_fields.items():
            if f.is_required() and k not in it:
                it[k] = ""
        if "evidence" in it and it["evidence"] not in ("tested", "sourced", "inferred", "unknown"):
            it["evidence"] = "inferred"
        if "actor" in it and it["actor"] not in ("agent", "human", "either"):
            it["actor"] = "human"
        try:
            out.append(model(**it))
        except Exception:  # noqa: BLE001
            continue
    return out


def _coerce_range(d, model):
    """TimeRange/WaitRange: keep only numeric fields that parse; a stray "unknown"
    in min_days should not kill the run."""
    if not isinstance(d, dict):
        return model()
    clean = {}
    for k, v in d.items():
        if k not in model.model_fields:
            continue
        if k.startswith(("min_", "max_")):
            try:
                clean[k] = int(float(v)) if v is not None and str(v).strip() else None
            except (TypeError, ValueError):
                clean[k] = None
        else:
            clean[k] = v
    try:
        return model(**clean)
    except Exception:  # noqa: BLE001
        return model(note=str(d.get("note") or "") or None)


def validate(raw: dict, pages: list[search.Page], info: dict, query: str,
             attempts: list[AgentAttempt]) -> Guide:
    """Build the Guide, enforcing citation discipline."""
    # match citations on a canonical form: Google docs redirect to ?hl=en, models cite the clean
    # URL; scheme/www/trailing-slash/fragment differences are not invented citations either
    fetched = {_canon(p.url): p for p in pages}
    official_urls = {u for u, p in fetched.items() if p.official}

    def fix(obj):
        """Recursively: unknown source_url → None + downgrade evidence."""
        if isinstance(obj, dict):
            if "source_url" in obj:
                u = _canon(obj.get("source_url") or "")
                if u and u not in fetched:
                    obj["source_url"] = None
                    if obj.get("evidence") == "sourced":
                        obj["evidence"] = "inferred"
                elif u:
                    obj["source_url"] = fetched[u].url  # normalise to the URL we actually fetched
                    if obj.get("evidence") == "sourced" and u not in official_urls:
                        # third-party source: keep the link, but it isn't authoritative
                        obj["evidence"] = "inferred"
                if obj.get("evidence") not in ("tested", "sourced", "inferred", "unknown"):
                    obj["evidence"] = "inferred"
            for v in obj.values():
                fix(v)
        elif isinstance(obj, list):
            for v in obj:
                fix(v)

    fix(raw)

    name = info.get("name") or query
    g = Guide(
        slug=slugify(name),
        name=name,
        website=info.get("website"),
        category=info.get("category"),
        developer_portal_url=raw.get("developer_portal_url") or info.get("developer_portal_url"),
        summary=raw.get("summary", ""),
        oauth_supported=raw.get("oauth_supported"),
        auth_methods=[str(a) for a in raw.get("auth_methods") or []],
        oauth_note=_coerce_claim(raw.get("oauth_note")),
        app_types=_coerce_list(raw.get("app_types"), AppType, ("name",)),
        registration_access=raw.get("registration_access") or "unknown",
        registration_access_claim=_coerce_claim(raw.get("registration_access_claim")),
        prerequisites=[c for c in map(_coerce_claim, raw.get("prerequisites") or []) if c],
        steps=_coerce_list(raw.get("steps"), Step, ("title",)),
        approval_gate=raw.get("approval_gate") or "unknown",
        approval_claim=_coerce_claim(raw.get("approval_claim")),
        unreviewed_limits=_coerce_claim(raw.get("unreviewed_limits")),
        review_artifacts=[c for c in map(_coerce_claim, raw.get("review_artifacts") or []) if c],
        approval_wait=_coerce_range(raw.get("approval_wait"), WaitRange),
        setup_time=_coerce_range(raw.get("setup_time"), TimeRange),
        redirect_uri_rules=_coerce_claim(raw.get("redirect_uri_rules")),
        token_lifecycle=_coerce_claim(raw.get("token_lifecycle")),
        scopes_note=_coerce_claim(raw.get("scopes_note")),
        instance_specific=_coerce_claim(raw.get("instance_specific")),
        pkce_required=_coerce_claim(raw.get("pkce_required")),
        blockers=_coerce_list(raw.get("blockers"), Blocker, ("blocker",)),
        agent_can=[str(x) for x in raw.get("agent_can") or []],
        human_must=[str(x) for x in raw.get("human_must") or []],
        composio_managed=info.get("composio_managed"),
        composio_note=info.get("composio_note"),
        sources=[Source(url=p.url, title=p.title, fetched_at=p.fetched_at, official=p.official) for p in pages],
        agent_attempts=attempts,
        unverified_claims=[str(x) for x in raw.get("unverified_claims") or []],
        generator=llm.settings.label if not llm.settings.mock else "mock",
        input_query=query,
    )
    g = merge_tested(g)
    g.difficulty = rubric.score(g)
    g.confidence = rubric.confidence(g)
    return g


def merge_tested(g: Guide) -> Guide:
    """If a human has recorded a real setup attempt in data/tested/<slug>.json,
    fold it in. Tested notes override the evidence tier of matching fields."""
    path = DATA / "tested" / f"{g.slug}.json"
    if not path.exists():
        return g
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return g
    for note in t.get("notes", []):
        g.tested_notes.append(Claim(text=note, evidence=Evidence.tested))
    if t.get("setup_minutes"):
        g.setup_time.min_minutes = t["setup_minutes"]
        g.setup_time.max_minutes = max(t["setup_minutes"], g.setup_time.max_minutes or 0)
        g.setup_time.evidence = Evidence.tested
        g.setup_time.note = t.get("setup_note") or g.setup_time.note
    for field in ("registration_access_claim", "approval_claim", "unreviewed_limits",
                  "redirect_uri_rules", "token_lifecycle"):
        if field in t and t[field]:
            setattr(g, field, Claim(text=t[field], evidence=Evidence.tested))
    if t.get("approval_gate"):
        g.approval_gate = ApprovalGate(t["approval_gate"])
    if t.get("registration_access"):
        g.registration_access = RegistrationAccess(t["registration_access"])
    for b in t.get("blockers", []):
        g.blockers.append(Blocker(blocker=b["blocker"], fix=b.get("fix", ""), evidence=Evidence.tested))
    return g


def investigate(query: str, progress: Progress | None = None, hints: list[str] | None = None) -> Guide:
    progress = progress or (lambda *a: None)

    if llm.settings.mock:
        return _mock(query, progress)

    info = _identify(query, progress)
    name = info["name"]
    progress("identify", f"Identified as {name}" + (f" ({info.get('website')})" if info.get("website") else ""),
             {"domains": sorted(info.get("official_roots") or [])})

    hits = _search(name, info.get("official_roots") or set(), progress, hints=hints,
                   portal=info.get("developer_portal_url"))
    # with a small prompt budget, fewer pages read properly beat many pages truncated to a paragraph
    page_cap = max(3, min(llm.settings.max_pages, llm.settings.max_source_chars // 3500))
    hits = _pick(name, hits, page_cap, progress)
    pages = _fetch(hits, page_cap, info.get("official_roots") or set(), progress)
    attempts = _probe(info, progress)

    progress("extract", f"Reading {len(pages)} pages and filling the guide…",
             {"official": sum(p.official for p in pages)})
    probes = "\n".join(f"- {a.action}: {a.result}" for a in attempts) or "(none)"
    budget = llm.settings.max_source_chars
    for attempt in range(5):
        try:
            raw = llm.chat_json(
                EXTRACT_SYSTEM,
                EXTRACT_USER.format(name=name, website=info.get("website") or "unknown",
                                    portal=info.get("developer_portal_url") or "unknown",
                                    probes=probes, sources=_sources_block(pages, budget)),
                max_tokens=llm.settings.max_output_tokens,
            )
            break
        except llm.RequestTooLarge as e:
            if attempt == 4 or budget <= 4000:
                raise
            # shrink by what the provider reported (with margin), and never by less than 30%,
            # because the reported ratio covers the whole prompt, not just the sources
            factor = min(e.ratio * 0.8, 0.7) if e.ratio else 0.5
            budget = max(int(budget * factor), 4000)
            progress("extract", f"Prompt too large for the model's limit; retrying with {budget:,} chars of sources",
                     None)
    progress("validate", "Checking citations and scoring difficulty", None)
    g = validate(raw, pages, info, query, attempts)
    progress("done", f"{g.name}: {g.difficulty.label} (confidence {g.confidence})",
             {"slug": g.slug, "level": g.difficulty.level})
    return g


def _mock(query: str, progress: Progress) -> Guide:
    """Canned run for UI development and demos without an API key."""
    import time
    sample = json.loads((DATA / "sample_guide.json").read_text(encoding="utf-8"))
    for stage, msg in [("identify", f"Looking up “{query}”"),
                       ("search", "Searched: OAuth app developer documentation"),
                       ("fetch", "Read official: Creating an OAuth app"),
                       ("probe", "Developer portal responded HTTP 200"),
                       ("extract", "Reading 6 pages and filling the guide…"),
                       ("validate", "Checking citations and scoring difficulty")]:
        progress(stage, msg, None)
        time.sleep(0.4)
    g = Guide(**sample)
    g.slug = slugify(query) if query else g.slug
    g.name = query or g.name
    g.input_query = query
    g.generator = "mock"
    g.summary = "MOCK MODE: this is a canned sample, not an investigation of " + g.name + ". " + g.summary
    g = merge_tested(g)
    g.difficulty = rubric.score(g)
    g.confidence = rubric.confidence(g)
    progress("done", f"{g.name}: {g.difficulty.label}", {"slug": g.slug})
    return g
