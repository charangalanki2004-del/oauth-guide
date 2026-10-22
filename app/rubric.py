"""
Difficulty rubric.

The LLM never picks the score. It fills structured fields; this module turns
them into a 1-5 level with the reasons listed, so two guides are comparable and
a reader can see exactly why an app is "Hard".

Points:
  who can register        anyone 0 | workspace_admin 1 | paid_tier 2 | partner_program 3
  approval gate           none 0 | marketplace_only 0.5 | soft_cap 1 | review_for_scopes 2.5
                          | review_required 3.5 | partner_only 4
  review artifacts        +0.5 each for privacy/terms/logo-type items, +1 demo video,
                          +1.5 business verification, +2 security assessment (max 3.5)
  instance-specific setup +0.5
  token lifecycle quirks  +0.5
  approval wait > 7 days  +0.5 ; > 30 days +1

Levels (by points):
  < 1      1 Trivial    (minutes, no review)
  1 - 2.5  2 Easy       (an hour or less, maybe a soft cap)
  2.5 - 4  3 Moderate   (a day of work, some review or admin involvement)
  4 - 6    4 Hard       (review with artifacts; weeks of calendar time)
  >= 6     5 Very hard  (partner program / security assessment / long queues)
"""
from __future__ import annotations

from .schema import ApprovalGate, Difficulty, Evidence, Guide, RegistrationAccess

ACCESS_POINTS = {
    RegistrationAccess.anyone: 0.0,
    RegistrationAccess.workspace_admin: 1.0,
    RegistrationAccess.paid_tier: 2.0,
    RegistrationAccess.partner_program: 3.0,
    RegistrationAccess.unknown: 0.5,
}

GATE_POINTS = {
    ApprovalGate.none: 0.0,
    ApprovalGate.marketplace_only: 0.5,
    ApprovalGate.soft_cap: 1.0,
    ApprovalGate.review_for_scopes: 2.5,
    ApprovalGate.review_required: 3.5,
    ApprovalGate.partner_only: 4.0,
    ApprovalGate.unknown: 1.0,
}

LABELS = {1: "Trivial", 2: "Easy", 3: "Moderate", 4: "Hard", 5: "Very hard"}

_ARTIFACT_WEIGHTS = [
    (("security assessment", "casa", "penetration", "pen test", "soc 2", "soc2"), 2.0, "security assessment"),
    (("business verification", "verify your business", "company verification", "kyb"), 1.5, "business verification"),
    (("demo video", "screencast", "video"), 1.0, "demo video"),
    (("privacy policy", "terms of service", "terms", "logo", "homepage", "domain verification",
      "scope justification", "justification", "questionnaire"), 0.5, None),
]


def _artifact_points(texts: list[str]) -> tuple[float, list[str]]:
    pts = 0.0
    reasons: list[str] = []
    seen: set[str] = set()
    for t in texts:
        low = t.lower()
        matched = False
        for keys, w, name in _ARTIFACT_WEIGHTS:
            if any(k in low for k in keys):
                label = name or t.strip()
                if label not in seen:
                    seen.add(label)
                    pts += w
                    if name:
                        reasons.append(f"Review requires a {name}")
                matched = True
                break
        if not matched and t.strip() not in seen:
            seen.add(t.strip())
            pts += 0.5
    return min(pts, 3.5), reasons


def score(guide: Guide) -> Difficulty:
    pts = 0.0
    reasons: list[str] = []

    if guide.oauth_supported is False:
        return Difficulty(score=0, level=1, label="No OAuth",
                          reasons=["This app does not offer OAuth; use its API key or other auth."])

    a = ACCESS_POINTS[guide.registration_access]
    pts += a
    if guide.registration_access == RegistrationAccess.workspace_admin:
        reasons.append("Needs a workspace or org admin to create the app")
    elif guide.registration_access == RegistrationAccess.paid_tier:
        reasons.append("Needs a paid developer tier")
    elif guide.registration_access == RegistrationAccess.partner_program:
        reasons.append("Needs enrolment in a partner program")
    elif guide.registration_access == RegistrationAccess.unknown:
        reasons.append("Could not confirm who is allowed to register an app")

    g = GATE_POINTS[guide.approval_gate]
    pts += g
    gate_reason = {
        ApprovalGate.none: "No review needed for private use",
        ApprovalGate.marketplace_only: "Review only if you list on the marketplace",
        ApprovalGate.soft_cap: "Works right away, but capped until reviewed",
        ApprovalGate.review_for_scopes: "Some scopes need review before real use",
        ApprovalGate.review_required: "Production use requires review",
        ApprovalGate.partner_only: "OAuth is restricted to approved partners",
        ApprovalGate.unknown: "Approval requirements could not be confirmed",
    }[guide.approval_gate]
    reasons.append(gate_reason)

    art_pts, art_reasons = _artifact_points([c.text for c in guide.review_artifacts])
    pts += art_pts
    reasons.extend(art_reasons)
    if guide.review_artifacts and not art_reasons:
        reasons.append(f"Review needs {len(guide.review_artifacts)} artifact(s) prepared")

    inst = (guide.instance_specific.text if guide.instance_specific else "").strip().lower()
    self_hosted = any(k in inst for k in ("enterprise server", "self-hosted", "self hosted", "on-premise", "on-prem"))
    if inst and not self_hosted and guide.instance_specific.evidence != Evidence.unknown \
            and not inst.startswith(("none", "no ", "not ")):
        pts += 0.5
        reasons.append("Tenant/instance-specific configuration")

    tl = (guide.token_lifecycle.text if guide.token_lifecycle else "").lower()
    benign = any(k in tl for k in ("do not expire", "don't expire", "never expire", "does not expire"))
    if tl and not benign and any(k in tl for k in ("expire", "rotate", "rotating", "short-lived", "7 days", "revoke")):
        pts += 0.5
        reasons.append("Token lifecycle has traps")

    w = guide.approval_wait
    if w.max_days:
        if w.max_days > 30:
            pts += 1.0
            reasons.append(f"Approval can take over a month ({w.max_days} days reported)")
        elif w.max_days > 7:
            pts += 0.5
            reasons.append(f"Approval can take more than a week ({w.max_days} days reported)")

    if pts < 1:
        level = 1
    elif pts < 2.5:
        level = 2
    elif pts < 4:
        level = 3
    elif pts < 6:
        level = 4
    else:
        level = 5

    return Difficulty(score=round(pts, 1), level=level, label=LABELS[level], reasons=reasons)


def confidence(guide: Guide) -> str:
    """How much to trust this guide overall, from the evidence tiers on key claims."""
    key = [
        guide.registration_access_claim,
        guide.approval_claim,
        guide.unreviewed_limits,
        guide.redirect_uri_rules,
        guide.token_lifecycle,
    ]
    tiers = [c.evidence for c in key if c is not None]
    tiers += [s.evidence for s in guide.steps]
    tiers.append(guide.setup_time.evidence)
    if guide.approval_gate not in (ApprovalGate.none, ApprovalGate.marketplace_only):
        tiers.append(guide.approval_wait.evidence)  # no review -> no wait to be uncertain about
    if not tiers:
        return "low"
    strong = sum(1 for t in tiers if t in (Evidence.tested, Evidence.sourced))
    ratio = strong / len(tiers)
    official = sum(1 for s in guide.sources if s.official)
    if ratio >= 0.7 and official >= 2:
        return "high"
    if ratio >= 0.4 and official >= 1:
        return "medium"
    return "low"
