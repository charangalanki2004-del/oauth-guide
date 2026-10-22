# Demo script (5 minutes)

Seed these before the demo; they span the setup archetypes:

| App | Why it is in the demo |
|---|---|
| GitHub | no review, any account, the "trivial" baseline; also shows the OAuth App vs GitHub App choice |
| Slack | workspace-scoped app, no review unless you list on the Marketplace |
| Google Workspace | soft cap in testing mode, sensitive/restricted scope verification, refresh-token expiry trap |
| QuickBooks Online | review required for production keys; security questionnaire |
| Shopify | partner account + per-shop install; instance-specific |

```bash
python scripts/seed.py --apps github,slack,google-workspace,quickbooks-online,shopify
```

Then:

1. Open the library. Point at the difficulty colours and the "Investigate" state on apps not yet generated.
2. Open GitHub → Google Workspace side by side. Same page structure, different verdict, reasons listed under the ladder.
3. Show the evidence tags and click one "source" link. Show "What the agent tried".
4. Ask the interviewer for an app. Paste it into Investigate. Narrate the stages as they stream: identify → search → read → probe → extract → check.
5. On the generated guide, open "Not verified" and "Sources" first: this is where honesty lives.
6. Copy link, open the Markdown export.

If the live run fails (free-tier model hiccup), click Investigate again; the UI explains the failure. Submitting a website URL instead of a name gives the identify step a better start.
