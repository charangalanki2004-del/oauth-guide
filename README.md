# OAuth app setup guides

Before an AI agent's users can connect an app, the developer often has to register their own OAuth app with the provider — and sometimes get it approved. This project tells a developer, for any app, **how hard that is, how long it takes, what will block them, and which parts an agent can do for them**, with every claim tagged by how it was verified.

- A **library** of 100 OAuth-capable apps; each gets a guide on first lookup.
- **Live investigation**: type a name or website and watch an agent find the developer docs, read them, probe what it can, and write a shareable guide.
- Every guide is a **structured object** with evidence tiers, not prose, so guides are comparable and auditable.

**Live demo:** https://oauth-guide.onrender.com · **Source:** https://github.com/charangalanki2004-del/oauth-guide
Guides: `/guides/<slug>` (e.g. [/guides/github](https://oauth-guide.onrender.com/guides/github)) · Markdown export: `/guides/<slug>.md` · JSON: `/api/guides/<slug>`

Hosted on Render's free tier: the first request after idle takes ~30 s to wake the service, and a live investigation takes 40–100 s on the free Groq model.

## Setup

```bash
git clone <repo> && cd oauth-guide
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then edit
uvicorn app.main:app --reload
```

Open http://localhost:8000.

**`.env` essentials**

| Variable | What |
|---|---|
| `LLM_PROVIDER` | `gemini` · `deepseek` · `groq` · `openai` · `anthropic` · `openrouter` · `custom` |
| `LLM_API_KEY` | key for that provider |
| `LLM_MODEL`, `LLM_BASE_URL` | optional overrides of the preset |
| `LLM_TPM` | the model's tokens-per-minute cap, if any; prompt/output budgets are derived from it (groq preset assumes 8000) and the agent shrinks + retries on "request too large" |
| `PUBLIC_BASE_URL` | used in "Copy link" (set on deploy) |
| `TAVILY_API_KEY` | optional; DuckDuckGo is used otherwise |
| `READER_FALLBACK` | default on: official pages with no extractable text (JavaScript-rendered docs) are re-fetched through r.jina.ai, a keyless rendering proxy; set `0` to disable |
| `COMPOSIO_API_KEY` | optional; lets the agent check whether a Composio-managed OAuth app exists |
| `MOCK_MODE=1` | run the UI with a canned investigation and no API calls |

Any OpenAI-compatible chat endpoint works. Free tiers of Gemini, Groq and DeepSeek have all been enough for this.

**Seed the library**

```bash
python scripts/seed.py --apps github,slack,google-workspace,quickbooks-online,shopify
python scripts/seed.py --all --missing        # every catalog app without a guide (slow)
```

**Record a real setup attempt** (strongest evidence): copy `data/tested/_template.json` to `data/tested/<slug>.json`, write only what you observed, then `python scripts/seed.py --apply-tested <slug>`.

**Audit a guide**: `python scripts/check.py github` prints a worksheet of every claim with its tier and source. `python scripts/check.py --summary` shows evidence quality across the library.

**Tests**: `python -m pytest`

**Deploy**: any host that runs `uvicorn app.main:app --host 0.0.0.0 --port $PORT` (a `Procfile` and `Dockerfile` are included). Set the env vars above. Commit `data/guides/*.json` so seeded guides survive redeploys; guides generated live on a host without a persistent disk are lost on restart.

## How it works

```
query ─► identify ─► search (6+ queries, official domains first) ─► pick (model chooses the reading list)
      ─► fetch (follows the docs' own "create an app" links; renders JS-only docs)
      ─► probe (OIDC discovery, portal reachability, Composio directory)
      ─► extract (LLM fills the Guide schema from fetched text only)
      ─► validate (strip uncited URLs, downgrade evidence, compute difficulty + confidence)
      ─► save JSON ─► render page / markdown / JSON
```

Progress streams to the browser over SSE, so a live run shows which pages were read and which probes succeeded.

**The guide schema** (`app/schema.py`) captures the seven axes on which providers actually differ: who can register, which app type to create, the approval gate, pre-review artifacts, instance/tenant quirks, token lifecycle, and redirect-URI rules. Plus steps (each marked agent / human / either), blockers with fixes, an agent-vs-human handoff list, and sources.

**Evidence tiers** are on every load-bearing claim:

| tier | meaning |
|---|---|
| tested | a human (via `data/tested/`) or the agent (probes) actually did it |
| sourced | an official provider page we fetched says so; link attached |
| inferred | the model's best reading; not confirmed |
| unknown | looked, could not find out |

The validator enforces this: the model may only cite URLs that were actually fetched (matched canonically, so `?hl=en` or a trailing slash is not a mismatch); a citation to anything else is removed and the claim is downgraded to *inferred*; citations to third-party pages keep the link but are also *inferred*. Overall **confidence** (low / medium / high) is computed from the share of sourced/tested claims and the number of official pages read.

**Difficulty** (`app/rubric.py`) is computed, never asked of the model. Points for registration access, approval gate, review artifacts (a security assessment weighs more than a privacy-policy URL), instance-specific setup, token traps, and long waits → level 1–5 with the reasons listed on the page. Two guides at "Hard" are hard for stated, comparable reasons.

## Key decisions

1. **Structured guide, not generated prose.** Developers need a five-second verdict and a comparable score. Prose can't be scored, diffed, or audited; a schema can. Prose is derived (markdown export) rather than primary.
2. **Score computed by a rubric, not the model.** LLM difficulty ratings are vibes. A rubric is explainable and consistent across 100 apps, and its weaknesses are fixable in one place.
3. **Citation discipline in code, not in the prompt.** Prompts asking for honesty don't prevent invented URLs. Restricting citations to the fetched set and downgrading anything else makes "sourced" mean something.
4. **Official domains first.** The identify step establishes the provider's own domains; search results from those rank first and only they can produce *sourced* claims. Blog posts (including vendor guides with stale callback URLs) can inform but never certify.
5. **Agent probes that are actually tests.** OIDC discovery documents, portal reachability, and the Composio directory are cheap, real checks. They are shown separately as "What the agent tried" so readers see the difference between reading docs and testing.
6. **Human-tested notes are a first-class input.** The highest tier can only come from a real attempt. `data/tested/` is the mechanism; the README there says to record only observations.
7. **The 100-app library is the same pipeline, not hand-written.** Any app, in or out of the catalog, goes through identical investigation; the catalog is just a list of names. A few catalog entries carry `search_hints` (extra search queries) because the product's name misleads search engines — "Google Workspace" returns Marketplace-listing docs, not Cloud Console OAuth docs. Hints change what is searched, never what is written; apps outside the catalog get none.
8. **Provider-agnostic LLM layer.** OpenAI-compatible client with presets, so a free Gemini/Groq/DeepSeek key works and the model can be swapped by env var.
9. **Composio-aware, not Composio-locked.** The guide notes whether a managed app exists and reminds readers to use the redirect URI from their auth config, but the steps are the provider's steps.

## What I checked, corrected, and could not verify

Three real registrations (GitHub, Slack, Google Cloud), timed and recorded in `data/tested/`, plus
spot-checks of sourced claims. Headlines: GitHub allows up to 10 redirect URIs and `http://localhost`
(guides said one, exact, HTTPS); Slack's "From scratch" flow no longer exists and the gate is
Marketplace-only; Google's console confirms the 100-user cap is lifetime and now offers an
"AI-powered agent" client type no docs mentioned. Full log, including what could not be verified
and why QuickBooks is deliberately left weak: [`docs/VERIFICATION.md`](docs/VERIFICATION.md).

## Limitations

- **Docs drift.** A guide is a snapshot; the page shows its generation date and offers re-investigation. Nothing re-checks automatically.
- **Approval wait times are the weakest claims.** Providers rarely publish them; most are *inferred* or *unknown* by design. Treat them as ranges, not promises.
- **Search quality bounds everything.** The agent follows the provider's own navigation from an official page to its "create an app" page, and re-fetches JavaScript-only docs through a rendering proxy, but docs behind a login are still invisible. Submitting a website URL instead of a name helps.
- **Some providers' docs cannot be read at all.** QuickBooks Online is the example kept in the demo: Intuit's help centre returns no text even through the rendering proxy, so the guide says `unknown` with `low` confidence instead of asserting the app-assessment requirement that exists in reality. The evidence tiers are there so this failure is visible, not hidden.
- **Provider UIs drift faster than their docs.** All three real registrations (GitHub, Slack, Google) differed from the documentation in menu names or form layout. Rules in a guide are more durable than button names; the *tested* tier records what the screen actually said on a given date.
- **Free-tier model budgets shape the agent.** On Groq (8k tokens/minute) the agent reads three pages per guide, chosen by the model; on a larger budget (`LLM_TPM` unset) it reads up to eight. Guide quality depends on that choice more than on the model.
- **Probes are shallow.** The agent does not log in, create apps, or run an authorization flow; it verifies endpoints and reachability. Registration still needs a human.
- **Rubric keyword matching** (e.g. detecting "security assessment" in artifact text) is simple and can miss paraphrases.
- **No persistence layer.** Guides are JSON files; on ephemeral hosts, live-generated guides disappear on restart unless committed.
- **One model call per stage, no retries on malformed JSON** beyond tolerant parsing. A flaky free-tier model can fail a run; the UI says so and lets you retry.
