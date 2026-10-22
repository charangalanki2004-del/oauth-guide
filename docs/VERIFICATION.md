# Verification log

How to use: after seeding, run `python scripts/check.py <slug>` for each app you
want to audit, open each source link, and record the verdict here. Keep it
honest: a short list of real checks beats a long list of claims.

## Apps audited

### GitHub (`github`)
- Real setup attempt: _(date, minutes, what you saw)_
- Confirmed against docs: _(list claims that matched)_
- Corrected: _(what the agent got wrong and what the source actually says)_
- Could not verify: _(claims with no authoritative source)_

### Slack (`slack`)
- Real setup attempt:
- Confirmed:
- Corrected:
- Could not verify:

### Google Workspace (`google-workspace`)
- Real setup attempt (test mode):
- Confirmed:
- Corrected:
- Could not verify:

### QuickBooks Online (`quickbooks-online`)
- Confirmed:
- Corrected:
- Could not verify:

### Shopify (`shopify`)
- Confirmed:
- Corrected:
- Could not verify:

## Patterns noticed

_(e.g. the agent reliably finds setup steps but rarely finds approval wait
times; vendor blog posts carried stale callback URLs; providers with multiple
app types confused the identify step until the website URL was given.)_

## Evidence summary

Paste the output of `python scripts/check.py --summary` here.
