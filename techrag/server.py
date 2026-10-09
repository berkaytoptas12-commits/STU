"""FastAPI app: JSON/SSE API + the offline single-page UI (no CDN, no external requests)."""

from __future__ import annotations

import json
import mimetypes
from contextlib import asynccontextmanager
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from techrag import __version__
from techrag.config import Config
from techrag.engine import RAGEngine
from techrag.ingest.loaders import SUPPORTED_SUFFIXES

WEB_DIR = Path(__file__).parent / "web"


class ChatTurn(BaseModel):
    role: str
    content: str


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = Field(default_factory=list)
    domains: Optional[list[str]] = None
    doc_ids: Optional[list[int]] = None
    top_k: Optional[int] = Field(default=None, ge=1, le=30)
    stream: bool = True


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    domains: Optional[list[str]] = None
    doc_ids: Optional[list[int]] = None
    top_k: Optional[int] = Field(default=None, ge=1, le=50)


class Jobs:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()  # one ingestion at a time

    def create(self, kind: str) -> dict:
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "queued", "messages": [],
               "report": None, "created": time.time()}
        self.items[job["id"]] = job
        return job


def _safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^\w.\- ()]+", "_", name, flags=re.UNICODE).strip(" .")
    return name or "upload"


def create_app(cfg: Config, engine: Optional[RAGEngine] = None, warmup: bool = True) -> FastAPI:
    engine = engine or RAGEngine(cfg)
    jobs = Jobs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if warmup:
            def run():
                try:
                    engine.warmup()
                except Exception as exc:  # surfaced via /api/info
                    app.state.warmup_error = str(exc)
            threading.Thread(target=run, daemon=True).start()
        yield

    # Swagger UI pulls assets from a CDN, which does not exist in an air-gapped network.
    app = FastAPI(title="techrag", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.engine = engine

    def auth(request: Request) -> None:
        token = cfg.server.api_token
        if not token:
            return
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        if supplied != token:
            raise HTTPException(status_code=401, detail="invalid or missing API token")

    # ------------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # ----------------------------------------------------------------- info
    @app.get("/api/info", dependencies=[Depends(auth)])
    def info():
        stats = engine.store.stats()
        return {
            "version": __version__,
            "llm": {"provider": cfg.llm.provider, "model": cfg.llm.model},
            "embedding": {"backend": cfg.embedding.backend, "model": cfg.embedding.model},
            "reranker": {"enabled": cfg.reranker.enabled, "model": cfg.reranker.model},
            "query_rewrite": cfg.retrieval.query_rewrite,
            "domains": [{"key": k, "name": engine.domains.name(k),
                         **stats["per_domain"].get(k, {"documents": 0, "chunks": 0})}
                        for k in engine.domains.keys()],
            "stats": stats,
            "allow_upload": cfg.server.allow_upload,
            "warmup_error": getattr(app.state, "warmup_error", None),
        }

    @app.get("/api/health", dependencies=[Depends(auth)])
    def health():
        return {"llm": engine.llm.health(), "index": engine.store.stats()["chunks"]}

    # ------------------------------------------------------------ documents
    @app.get("/api/documents", dependencies=[Depends(auth)])
    def documents():
        return [{"id": d.id, "title": d.title, "domain": d.domain, "path": d.path, "n_pages": d.n_pages,
                 "n_chunks": d.n_chunks, "warnings": d.warnings, "ingested_at": d.ingested_at,
                 "has_toc": bool(d.toc)} for d in engine.store.documents()]

    @app.get("/api/documents/{doc_id}", dependencies=[Depends(auth)])
    def document(doc_id: int):
        d = engine.store.document(doc_id)
        if not d:
            raise HTTPException(404, "document not found")
        return {"id": d.id, "title": d.title, "domain": d.domain, "path": d.path, "n_pages": d.n_pages,
                "n_chunks": d.n_chunks, "toc": d.toc, "metadata": d.metadata, "warnings": d.warnings,
                "ingested_at": d.ingested_at}

    @app.get("/api/documents/{doc_id}/file", dependencies=[Depends(auth)])
    def document_file(doc_id: int):
        d = engine.store.document(doc_id)
        if not d:
            raise HTTPException(404, "document not found")
        path = Path(d.path)
        if not path.is_absolute():
            path = (cfg.sources_dir / path).resolve()
            if cfg.sources_dir.resolve() not in path.parents:
                raise HTTPException(403, "path outside sources dir")
        if not path.exists():
            raise HTTPException(404, "file no longer exists")
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, filename=path.name, content_disposition_type="inline")

    @app.get("/api/chunks/{chunk_id}", dependencies=[Depends(auth)])
    def chunk(chunk_id: int):
        rows = engine.store.chunks_by_ids([chunk_id])
        if chunk_id not in rows:
            raise HTTPException(404, "chunk not found")
        return rows[chunk_id].__dict__

    # --------------------------------------------------------------- search
    @app.post("/api/search", dependencies=[Depends(auth)])
    def search(req: SearchRequest):
        res = engine.retrieve(req.query, domains=req.domains, doc_ids=req.doc_ids, top_k=req.top_k)
        return {"plan": res.plan.to_dict(), "routed_domains": res.routed_domains, "confidence": res.confidence,
                "timings": res.timings, "sources": [p.to_dict() for p in res.passages]}

    @app.post("/api/ask", dependencies=[Depends(auth)])
    def ask(req: AskRequest):
        history = [t.model_dump() if hasattr(t, "model_dump") else t.dict() for t in req.history]
        if not req.stream:
            return engine.ask(req.question, history, req.domains, req.doc_ids, req.top_k).to_dict()

        def events():
            try:
                for ev in engine.ask_stream(req.question, history, req.domains, req.doc_ids, req.top_k):
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except Exception as exc:
                err = {"type": "error", "message": f"{exc.__class__.__name__}: {exc}"}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------ ingestion
    def start_job(kind: str, target: Optional[Path]) -> dict:
        from techrag.ingest.pipeline import Ingestor

        job = jobs.create(kind)

        def run():
            with jobs.lock:
                job["status"] = "running"
                try:
                    ing = Ingestor(cfg, engine.store, engine.embedder, engine.domains,
                                   progress=lambda m: job["messages"].append(m))
                    report = ing.run(target, prune=target is None)
                    job["report"] = {"summary": report.summary(), "failed": report.failed,
                                     "warnings": report.warnings}
                    job["status"] = "failed" if report.failed else "done"
                except Exception as exc:
                    job["messages"].append(f"ERROR: {exc}")
                    job["status"] = "failed"

        threading.Thread(target=run, daemon=True).start()
        return job

    @app.post("/api/upload", dependencies=[Depends(auth)])
    async def upload(file: UploadFile = File(...), domain: str = Form("general")):
        if not cfg.server.allow_upload:
            raise HTTPException(403, "upload disabled (server.allow_upload)")
        if domain not in engine.domains.keys():
            raise HTTPException(400, f"unknown collection '{domain}'")
        name = _safe_filename(file.filename or "upload")
        if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise HTTPException(400, f"unsupported file type; allowed: {', '.join(sorted(SUPPORTED_SUFFIXES))}")
        dest_dir = cfg.sources_dir / domain
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        with open(dest, "wb") as fh:
            while chunk_bytes := await file.read(1 << 20):
                fh.write(chunk_bytes)
        job = start_job("upload", dest)
        return {"job": job["id"], "path": str(dest)}

    @app.post("/api/ingest", dependencies=[Depends(auth)])
    def ingest_all():
        if not cfg.server.allow_upload:
            raise HTTPException(403, "ingestion from the UI is disabled (server.allow_upload)")
        return {"job": start_job("ingest", None)["id"]}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
    def job_status(job_id: str):
        job = jobs.items.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return JSONResponse(job)

    return app
