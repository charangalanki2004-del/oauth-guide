#!/usr/bin/env python
"""
Print a verification worksheet for one or more guides: every load-bearing
claim with its evidence tier and source, so you can open the source and tick
it off. Also summarizes evidence quality across the library.

  python scripts/check.py github            # worksheet for one guide
  python scripts/check.py --summary         # tier counts for all guides
  python scripts/check.py github > docs/verification/github.md
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252; the output has arrows/ticks
    sys.stdout.reconfigure(encoding="utf-8")

from app import store  # noqa: E402


def worksheet(slug: str) -> None:
    g = store.load(slug)
    if not g:
        print(f"no guide: {slug}")
        return
    print(f"# Verification worksheet: {g.name}\n")
    print(f"Generated {g.generated_at[:10]} by {g.generator}. Difficulty {g.difficulty.level} "
          f"({g.difficulty.label}), confidence {g.confidence}.\n")
    print("Mark each row: ✅ confirmed · ✏️ corrected (say what) · ❓ could not verify\n")
    print("| # | Field | Claim | Tier | Source | Verdict |")
    print("|---|---|---|---|---|---|")
    rows = []

    def add(field, c):
        if c and getattr(c, "text", ""):
            rows.append((field, c.text, c.evidence.value, c.source_url or ""))

    add("registration access", g.registration_access_claim)
    add("approval gate", g.approval_claim)
    add("unreviewed limits", g.unreviewed_limits)
    for c in g.review_artifacts:
        add("review artifact", c)
    rows.append(("approval wait", f"{g.approval_wait.min_days}–{g.approval_wait.max_days} days; {g.approval_wait.note or ''}",
                 g.approval_wait.evidence.value, g.approval_wait.source_url or ""))
    rows.append(("setup time", f"{g.setup_time.min_minutes}–{g.setup_time.max_minutes} min; {g.setup_time.note or ''}",
                 g.setup_time.evidence.value, g.setup_time.source_url or ""))
    add("redirect uri rules", g.redirect_uri_rules)
    add("token lifecycle", g.token_lifecycle)
    add("scopes", g.scopes_note)
    add("pkce", g.pkce_required)
    add("instance", g.instance_specific)
    for s in g.steps:
        rows.append((f"step: {s.title}", s.detail, s.evidence.value, s.source_url or ""))
    for b in g.blockers:
        rows.append((f"blocker: {b.blocker}", b.fix, b.evidence.value, b.source_url or ""))
    for i, (f, t, e, s) in enumerate(rows, 1):
        t = t.replace("|", "\\|").replace("\n", " ")
        print(f"| {i} | {f} | {t} | {e} | {s} |  |")
    if g.unverified_claims:
        print("\nThe agent itself flagged as unverified:\n")
        for x in g.unverified_claims:
            print(f"- {x}")
    print("\nSources read:\n")
    for s in g.sources:
        print(f"- {'official' if s.official else 'third-party'}: {s.url}")


def summary() -> None:
    guides = store.all_guides()
    print(f"{len(guides)} guides\n")
    print("| Guide | Level | Gate | Confidence | tested | sourced | inferred | unknown | official sources |")
    print("|---|---|---|---|---|---|---|---|---|")
    for g in guides:
        c = Counter()
        for cl in [g.registration_access_claim, g.approval_claim, g.unreviewed_limits, g.redirect_uri_rules,
                   g.token_lifecycle, g.scopes_note, g.pkce_required] + list(g.review_artifacts) + list(g.prerequisites):
            if cl:
                c[cl.evidence.value] += 1
        for s in g.steps:
            c[s.evidence.value] += 1
        for b in g.blockers:
            c[b.evidence.value] += 1
        off = sum(1 for s in g.sources if s.official)
        print(f"| {g.name} | {g.difficulty.level} | {g.approval_gate.value} | {g.confidence} | "
              f"{c['tested']} | {c['sourced']} | {c['inferred']} | {c['unknown']} | {off}/{len(g.sources)} |")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
    elif sys.argv[1] == "--summary":
        summary()
    else:
        for s in sys.argv[1:]:
            worksheet(s)
