#!/usr/bin/env python
"""
Seed the library.

  python scripts/seed.py --apps github,slack,google-workspace    # investigate these
  python scripts/seed.py --all                                   # all 100 (slow; costs API calls)
  python scripts/seed.py --all --missing                         # only apps without a guide yet
  python scripts/seed.py --apply-tested github                   # merge data/tested/github.json into the existing guide

Each run prints the stages so you can watch what the agent reads.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252; the output has arrows/ticks
    sys.stdout.reconfigure(encoding="utf-8")

from app import agent, rubric, store  # noqa: E402
from app.schema import Evidence  # noqa: E402


def progress(stage: str, msg: str, data):
    print(f"  [{stage:8}] {msg}")


def investigate(name_or_slug: str) -> None:
    entry = store.find_in_catalog(name_or_slug)
    query = entry["website"] if entry and entry.get("website") else name_or_slug
    label = entry["name"] if entry else name_or_slug
    print(f"\n== {label} ({query})")
    t0 = time.time()
    try:
        g = agent.investigate(query, progress, hints=(entry or {}).get("search_hints"))
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {type(e).__name__}: {e}")
        return
    if entry:
        g.slug = entry["slug"]
        g.name = entry["name"]
        g.category = g.category or entry.get("category")
    p = store.save(g)
    print(f"  saved {p.name}  level={g.difficulty.level} {g.difficulty.label}  "
          f"confidence={g.confidence}  sources={len(g.sources)}  {time.time()-t0:.0f}s")


def apply_tested(slug: str) -> None:
    g = store.load(slug)
    if not g:
        print(f"no guide for {slug}; run --apps {slug} first")
        return
    g.tested_notes = []
    g.blockers = [b for b in g.blockers if b.evidence != Evidence.tested]
    g = agent.merge_tested(g)
    g.difficulty = rubric.score(g)
    g.confidence = rubric.confidence(g)
    store.save(g)
    print(f"applied tested notes to {slug}: {len(g.tested_notes)} notes, confidence={g.confidence}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", help="comma-separated names or slugs")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--missing", action="store_true", help="with --all: skip apps that already have a guide")
    ap.add_argument("--apply-tested", help="slug whose data/tested/<slug>.json should be merged")
    a = ap.parse_args()

    if a.apply_tested:
        apply_tested(a.apply_tested)
        return
    targets: list[str] = []
    if a.apps:
        targets = [s.strip() for s in a.apps.split(",") if s.strip()]
    elif a.all:
        targets = [e["slug"] for e in store.catalog() if not (a.missing and e["generated"])]
    else:
        ap.print_help()
        return
    for t in targets:
        investigate(t)


if __name__ == "__main__":
    main()
