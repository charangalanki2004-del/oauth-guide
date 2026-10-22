# Human-tested notes

When you actually register an OAuth app with a provider, record what you saw
here as `data/tested/<slug>.json`. The agent merges these into the guide and
marks the affected fields **tested**, which is the strongest evidence tier.

Only write what you observed. If it matches the docs, say so; if it differs,
that difference is the most valuable thing in the whole guide.

Template (`data/tested/_template.json`):

```json
{
  "slug": "github",
  "tested_on": "2026-10-20",
  "setup_minutes": 12,
  "setup_note": "Timed from opening Settings to having client ID + secret.",
  "registration_access": "anyone",
  "approval_gate": "none",
  "registration_access_claim": "Registered under a personal account with no prompts about plans or verification.",
  "approval_claim": "No review step anywhere in the flow; the app worked immediately.",
  "unreviewed_limits": "Consent screen showed no warning banner.",
  "redirect_uri_rules": "Accepted http://localhost:8000/callback; rejected a second URL (one per app).",
  "token_lifecycle": "",
  "blockers": [
    {"blocker": "Client secret is shown only once", "fix": "Copy it before leaving the page; otherwise regenerate."}
  ],
  "notes": [
    "The 'Enable Device Flow' checkbox is off by default; not needed for a web redirect flow.",
    "Composio's dashboard showed the v3 callback URL; older blog posts show a v1 URL that no longer works."
  ]
}
```

Fields other than `slug` are optional. After adding or editing a file, run
`python scripts/seed.py --apply-tested <slug>` to merge it into the existing
guide (or re-investigate the app; new investigations merge it automatically).
