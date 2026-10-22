"""
Routes
  GET  /                      library: catalog of OAuth apps + generated guides
  GET  /guides/{slug}         shareable guide page
  GET  /guides/{slug}.md      markdown export
  GET  /api/guides/{slug}     JSON
  POST /api/generate          start an investigation  -> {job_id}
  GET  /api/jobs/{id}/events  SSE stream of progress; ends with "done" or "error"
  GET  /generate?q=           page that starts a job and shows progress live
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from urllib.parse import quote
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import agent, llm, store
from .schema import slugify

HERE = Path(__file__).resolve().parent
app = FastAPI(title="OAuth app setup guides")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["base_url"] = llm.settings.public_base_url
templates.env.globals["llm_label"] = "mock" if llm.settings.mock else llm.settings.label
templates.env.globals["llm_ready"] = llm.settings.ready()

# job_id -> {"q": Queue, "status": str, "slug": str|None, "error": str|None}
JOBS: dict[str, dict] = {}


def _run_job(job_id: str, query: str) -> None:
    job = JOBS[job_id]
    q: queue.Queue = job["q"]

    def progress(stage: str, message: str, data: dict | None) -> None:
        q.put({"stage": stage, "message": message, "data": data or {}})

    try:
        entry = store.find_in_catalog(query)
        g = agent.investigate(query, progress, hints=(entry or {}).get("search_hints"))
        if entry:  # same pinning scripts/seed.py does, so library cards light up
            g.slug, g.name = entry["slug"], entry["name"]
            g.category = g.category or entry.get("category")
        store.save(g)
        job["slug"] = g.slug
        job["status"] = "done"
        q.put({"stage": "done", "message": "Guide ready", "data": {"slug": g.slug}})
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        q.put({"stage": "error", "message": job["error"], "data": {}})
    finally:
        q.put(None)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cat = store.catalog()
    guides = {g.slug: g for g in store.all_guides()}
    # guides generated for apps not in the catalog (live submissions)
    extra = [g for s, g in guides.items() if s not in {a["slug"] for a in cat}]
    order = ["developer", "productivity", "communication", "crm", "storage", "finance", "commerce",
             "marketing", "social", "support", "hr", "analytics", "other"]
    present = {a.get("category", "other") for a in cat}
    cats = [c for c in order if c in present] + sorted(present - set(order))
    return templates.TemplateResponse(request, "index.html", {
        "catalog": cat, "guides": guides, "extra": extra, "categories": cats,
        "generated_count": len(guides),
    })


@app.get("/guides/{slug}.md", response_class=PlainTextResponse)
def guide_md(slug: str):
    g = store.load(slug)
    if not g:
        raise HTTPException(404)
    return store.to_markdown(g, llm.settings.public_base_url)


@app.get("/guides/{slug}", response_class=HTMLResponse)
def guide(request: Request, slug: str):
    g = store.load(slug)
    if not g:
        entry = store.find_in_catalog(slug)
        q = (entry.get("website") or entry["name"]) if entry else slug
        return RedirectResponse(f"/generate?q={quote(q, safe='')}", status_code=302)
    return templates.TemplateResponse(request, "guide.html", {"g": g})


@app.get("/api/guides/{slug}")
def guide_json(slug: str):
    g = store.load(slug)
    if not g:
        raise HTTPException(404)
    return JSONResponse(g.model_dump(mode="json"))


@app.get("/api/guides")
def guides_json():
    return [{"slug": g.slug, "name": g.name, "difficulty": g.difficulty.model_dump(),
             "approval_gate": g.approval_gate, "confidence": g.confidence} for g in store.all_guides()]


@app.post("/api/generate")
async def start(request: Request):
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    if not llm.settings.ready():
        raise HTTPException(503, "No LLM configured. Set LLM_PROVIDER and LLM_API_KEY in .env, or MOCK_MODE=1.")
    force = bool(body.get("force"))
    entry = store.find_in_catalog(query)
    existing = store.load(entry["slug"] if entry else slugify(query))
    if existing and not force:
        return {"job_id": None, "slug": existing.slug, "cached": True}
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"q": queue.Queue(), "status": "running", "slug": None, "error": None, "query": query}
    threading.Thread(target=_run_job, args=(job_id, query), daemon=True).start()
    return {"job_id": job_id, "cached": False}


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404)
    q: queue.Queue = job["q"]

    async def gen():
        loop = asyncio.get_event_loop()
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/generate", response_class=HTMLResponse)
def generate_page(request: Request, q: str = ""):
    return templates.TemplateResponse(request, "generate.html", {"q": q})


@app.get("/health")
def health():
    return {"ok": True, "llm": templates.env.globals["llm_label"], "ready": llm.settings.ready(),
            "guides": len(store.all_guides())}
