"""Guides live as JSON files in data/guides/. No database: the files are the
shareable artifact, they diff nicely in git, and the seed script regenerates them."""
from __future__ import annotations

import json
from pathlib import Path

from .schema import Evidence, Guide, slugify

DATA = Path(__file__).resolve().parent.parent / "data"
GUIDES = DATA / "guides"
GUIDES.mkdir(parents=True, exist_ok=True)


def save(g: Guide) -> Path:
    p = GUIDES / f"{g.slug}.json"
    p.write_text(json.dumps(g.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def load(slug: str) -> Guide | None:
    p = GUIDES / f"{slug}.json"
    if not p.exists():
        return None
    return Guide(**json.loads(p.read_text(encoding="utf-8")))


def all_guides() -> list[Guide]:
    out = []
    for p in sorted(GUIDES.glob("*.json")):
        try:
            out.append(Guide(**json.loads(p.read_text(encoding="utf-8"))))
        except Exception:  # noqa: BLE001
            continue
    return out


def catalog() -> list[dict]:
    """The 100-app library. Each entry gets `slug` and `generated` (bool)."""
    apps = json.loads((DATA / "apps.json").read_text(encoding="utf-8"))
    have = {p.stem for p in GUIDES.glob("*.json")}
    for a in apps:
        a["slug"] = a.get("slug") or slugify(a["name"])
        a["generated"] = a["slug"] in have
    return apps


def find_in_catalog(query: str) -> dict | None:
    q = query.strip().lower()
    s = slugify(query)
    for a in catalog():
        if a["slug"] == s or a["name"].lower() == q or (a.get("website") and a["website"].lower().rstrip("/") == q.rstrip("/")):
            return a
    return None


# ----------------------------------------------------------------- markdown --

_TIER = {Evidence.tested: "tested", Evidence.sourced: "sourced", Evidence.inferred: "inferred", Evidence.unknown: "unknown"}


def _c(claim, label: str) -> str:
    if not claim or not claim.text:
        return ""
    tier = _TIER.get(claim.evidence, "inferred")
    src = f" ([source]({claim.source_url}))" if claim.source_url else ""
    return f"- **{label}:** {claim.text} _[{tier}]_{src}\n"


def to_markdown(g: Guide, base_url: str = "") -> str:
    d = g.difficulty
    st = g.setup_time
    aw = g.approval_wait
    lines = [f"# {g.name} — OAuth app setup guide\n"]
    if g.website:
        lines.append(f"{g.website}  \n")
    lines.append(f"**Difficulty:** {d.level}/5 {d.label}  \n")
    if st.min_minutes or st.max_minutes:
        lines.append(f"**Hands-on time:** {st.min_minutes or '?'}–{st.max_minutes or '?'} min _[{_TIER.get(st.evidence)}]_  \n")
    if aw.max_days:
        lines.append(f"**Approval wait:** {aw.min_days or 0}–{aw.max_days} days _[{_TIER.get(aw.evidence)}]_  \n")
    elif aw.note:
        lines.append(f"**Approval wait:** {aw.note} _[{_TIER.get(aw.evidence)}]_  \n")
    lines.append(f"**Approval gate:** {g.approval_gate.value}  \n**Confidence:** {g.confidence}\n")
    lines.append(f"\n{g.summary}\n")
    if d.reasons:
        lines.append("\n## Why this difficulty\n" + "".join(f"- {r}\n" for r in d.reasons))
    if g.oauth_supported is False:
        lines.append("\n**This app does not appear to support OAuth.**\n")
        lines.append(_c(g.oauth_note, "Auth"))
    if g.app_types:
        lines.append("\n## Which app type to create\n")
        for t in g.app_types:
            rec = " (recommended for agents)" if t.recommended_for_agents else ""
            lines.append(f"- **{t.name}**{rec}: {t.when_to_use}\n")
    if g.prerequisites:
        lines.append("\n## Before you start\n")
        for c in g.prerequisites:
            lines.append(_c(c, "Need"))
    lines.append("\n## Registration\n")
    lines.append(_c(g.registration_access_claim, "Who can register"))
    if g.developer_portal_url:
        lines.append(f"- **Portal:** {g.developer_portal_url}\n")
    if g.steps:
        lines.append("\n## Steps\n")
        for i, s in enumerate(g.steps, 1):
            src = f" ([source]({s.source_url}))" if s.source_url else ""
            lines.append(f"{i}. **{s.title}** — {s.detail} _[{s.actor.value} · {_TIER.get(s.evidence)}]_{src}\n")
    lines.append("\n## Approval\n")
    lines.append(_c(g.approval_claim, "Gate"))
    lines.append(_c(g.unreviewed_limits, "Before approval"))
    if g.review_artifacts:
        lines.append("- **You will need:**\n")
        for c in g.review_artifacts:
            lines.append("  " + _c(c, "").replace("- ****", "- "))
    lines.append("\n## Nuances\n")
    lines.append(_c(g.redirect_uri_rules, "Redirect URIs"))
    lines.append(_c(g.token_lifecycle, "Tokens"))
    lines.append(_c(g.scopes_note, "Scopes"))
    lines.append(_c(g.pkce_required, "PKCE"))
    lines.append(_c(g.instance_specific, "Instance specifics"))
    if g.blockers:
        lines.append("\n## If you get stuck\n")
        for b in g.blockers:
            lines.append(f"- **{b.blocker}** → {b.fix} _[{_TIER.get(b.evidence)}]_\n")
    lines.append("\n## What an agent can do vs. what you must do\n")
    if g.agent_can:
        lines.append("**Agent can:**\n" + "".join(f"- {x}\n" for x in g.agent_can))
    if g.human_must:
        lines.append("\n**You must:**\n" + "".join(f"- {x}\n" for x in g.human_must))
    if g.composio_note:
        lines.append(f"\n## Composio\n{g.composio_note}\n")
    if g.tested_notes:
        lines.append("\n## Tested by a human\n" + "".join(f"- {c.text}\n" for c in g.tested_notes))
    if g.agent_attempts:
        lines.append("\n## What the agent tried\n")
        for a in g.agent_attempts:
            mark = "✓" if a.ok else ("✗" if a.ok is False else "–")
            lines.append(f"- {mark} {a.action}: {a.result}\n")
    if g.unverified_claims:
        lines.append("\n## Not verified\n" + "".join(f"- {x}\n" for x in g.unverified_claims))
    if g.sources:
        lines.append("\n## Sources\n")
        for s in g.sources:
            tag = "official" if s.official else "third-party"
            lines.append(f"- [{s.title or s.url}]({s.url}) ({tag}, fetched {(s.fetched_at or '')[:10]})\n")
    lines.append(f"\n---\nGenerated {g.generated_at[:10]} by {g.generator}. ")
    if base_url:
        lines.append(f"Live version: {base_url}/guides/{g.slug}\n")
    return "".join(lines)
