"""FastAPI backend for the desktop window (and optional browser use). Serves the offline UI and the API."""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from techrag import __version__, tls
from techrag.api import APIClient, check_service, configure_tls, current_tls, normalize_base_url
from techrag.config import Config, ServiceConfig, resource_path
from techrag.engine import RAGEngine
from techrag.ingest.loaders import SUPPORTED_SUFFIXES
from techrag.settings import MASK, public_settings, settings_path, update_settings

WEB_DIR = resource_path("web")
SERVICES = ("llm", "vision", "embedding", "reranker")


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


class ServiceProbe(BaseModel):
    service: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    verify_ssl: Optional[bool] = None


class CertRequest(BaseModel):
    base_url: str
    sha256: Optional[str] = None


class LibraryOpen(BaseModel):
    path: str
    read_only: bool = False


class PathsRequest(BaseModel):
    paths: list[str]
    collection: str = "general"


class PublishRequest(BaseModel):
    dest: str


class IngestRequest(BaseModel):
    rebuild: bool = False


class Jobs:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()

    def create(self, kind: str) -> dict:
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "queued", "messages": [], "report": None,
               "created": time.time()}
        self.items[job["id"]] = job
        return job


def _safe_filename(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\- ()]+", "_", name, flags=re.UNICODE).strip(" .") or "upload"


def create_app(cfg: Config, engine: Optional[RAGEngine] = None, warmup: bool = True,
               desktop: bool = False) -> FastAPI:
    jobs = Jobs()
    state = {"cfg": cfg, "engine": engine, "error": None}
    engine_lock = threading.Lock()

    def build_engine(c: Config) -> None:
        try:
            state["engine"] = RAGEngine(c)
            state["error"] = None
        except Exception as exc:
            state["engine"] = None
            state["error"] = f"{exc.__class__.__name__}: {exc}"

    if engine is None:
        build_engine(cfg)

    def eng() -> RAGEngine:
        e = state["engine"]
        if e is None:
            raise HTTPException(503, state["error"] or "library not available")
        return e

    def warm():
        e = state["engine"]
        if not e:
            return
        try:
            e.store.vectors()
        except Exception as exc:
            state["error"] = str(exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if warmup:
            threading.Thread(target=warm, daemon=True).start()
        yield

    # Swagger UI pulls assets from a CDN, which does not exist in an air-gapped network.
    app = FastAPI(title="techrag", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.state.state = state

    def auth(request: Request) -> None:
        token = state["cfg"].server.api_token
        if not token:
            return
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        if supplied != token:
            raise HTTPException(status_code=401, detail="invalid or missing API token")

    guard = [Depends(auth)]
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    # ------------------------------------------------------------------ info
    @app.get("/api/info", dependencies=guard)
    def info():
        c: Config = state["cfg"]
        e = state["engine"]
        stats = e.store.stats() if e else {"documents": 0, "chunks": 0, "tables": 0, "parameters": 0,
                                           "per_domain": {}}
        domains = []
        if e:
            for k in e.domains.keys():
                domains.append({"key": k, "name": e.domains.name(k),
                                **stats["per_domain"].get(k, {"documents": 0, "chunks": 0})})
            for k, v in stats["per_domain"].items():
                if k not in e.domains.keys():
                    domains.append({"key": k, "name": k, **v})
        return {"version": __version__, "desktop": desktop, "library": str(c.library_dir.resolve()),
                "read_only": c.read_only, "language": c.ui.language,
                "legacy_documents": stats.get("legacy_documents", 0),
                "models": {s: getattr(c, s).model for s in SERVICES},
                "stats": stats, "domains": domains, "error": state["error"],
                "allow_upload": c.server.allow_upload and not c.read_only}

    # -------------------------------------------------------------- settings
    @app.get("/api/settings", dependencies=guard)
    def get_settings():
        return public_settings(state["cfg"])

    @app.put("/api/settings", dependencies=guard)
    def put_settings(patch: dict):
        with engine_lock:
            try:
                new_cfg = update_settings(state["cfg"], patch)
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, str(exc))
            new_cfg.server = state["cfg"].server
            state["cfg"] = new_cfg
            configure_tls(new_cfg.tls)
            if set(patch) - {"ui"} or state["engine"] is None:
                build_engine(new_cfg)
            else:
                state["engine"].cfg = new_cfg
        return {"settings": public_settings(new_cfg), "error": state["error"]}

    def _probe_service(p: ServiceProbe) -> ServiceConfig:
        if p.service not in SERVICES:
            raise HTTPException(400, f"unknown service '{p.service}'")
        c: Config = state["cfg"]
        saved = c.vision_service() if p.service == "vision" else getattr(c, p.service)
        key = saved.api_key if (p.api_key in (None, MASK)) else p.api_key
        return ServiceConfig(base_url=p.base_url if p.base_url is not None else saved.base_url,
                             api_key=key or "", model=p.model if p.model is not None else saved.model,
                             timeout=30.0, verify_ssl=saved.verify_ssl if p.verify_ssl is None else p.verify_ssl)

    @app.post("/api/settings/models", dependencies=guard)
    def list_models(p: ServiceProbe):
        svc = _probe_service(p)
        try:
            return {"ok": True, "models": APIClient(svc).list_models()}
        except Exception as exc:
            return {"ok": False, "models": [], "error": str(exc), "code": getattr(exc, "code", "") or "error"}

    def _https_target(base_url: str) -> tuple[str, int]:
        host, port, scheme = tls.host_port(normalize_base_url(base_url))
        if scheme != "https" or not host:
            raise HTTPException(400, "only https:// endpoints have a certificate")
        return host, port

    @app.post("/api/settings/certificate", dependencies=guard)
    def certificate(req: CertRequest):
        """Certificates the server presents (not verified) + whether the current trust settings accept them."""
        host, port = _https_target(req.base_url)
        try:
            chain = tls.fetch_chain(host, port)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "code": tls.classify(exc)}
        t = current_tls()
        ok, code, msg = tls.check_handshake(host, port, tls.build_context(t.system_store, t.ca_bundle))
        return {"ok": True, "host": host, "port": port, "chain": [tls.describe(c).to_dict() for c in chain],
                "verify": {"ok": ok, "code": code, "error": msg}}

    @app.post("/api/settings/trust", dependencies=guard)
    def trust(req: CertRequest):
        """Save the server's certificate chain and add it to the trusted files - only if it is still the
        certificate whose fingerprint the user confirmed."""
        host, port = _https_target(req.base_url)
        if not req.sha256:
            raise HTTPException(400, "sha256 fingerprint of the confirmed certificate is required")
        chain = tls.fetch_chain(host, port)
        if not chain or tls.describe(chain[0]).sha256 != req.sha256.strip().upper():
            raise HTTPException(409, "the server certificate changed since it was shown; check again")
        path = tls.save_chain(chain, settings_path().parent / "certs", host, port)
        files = [f.strip() for f in re.split(r"[;\n]", state["cfg"].tls.ca_bundle or "") if f.strip()]
        if str(path) not in files:
            files.append(str(path))
        res = put_settings({"tls": {"ca_bundle": ";".join(files)}})
        return {"ok": True, "path": str(path), **res}

    @app.post("/api/settings/test", dependencies=guard)
    def test_service(p: ServiceProbe):
        svc = _probe_service(p)
        result = check_service(svc)
        if not result["ok"] or not svc.model:
            return result
        override = {"base_url": svc.base_url, "api_key": svc.api_key, "model": svc.model,
                    "verify_ssl": svc.verify_ssl}
        t = time.time()
        try:
            c: Config = state["cfg"]
            if p.service in ("llm", "vision"):
                from techrag.llm import LLMClient

                client = LLMClient(c.llm, service=svc)
                txt = client.chat([{"role": "user", "content": "Reply with the single word OK."}],
                                  max_tokens=16, thinking=False).content
                result["detail"] = f"reply: {txt[:40]!r}"
            elif p.service == "embedding":
                from techrag.embeddings import APIEmbedder

                ec = c.embedding.__class__(**{**c.embedding.__dict__, **override})
                result["detail"] = f"dimension {APIEmbedder(ec).dim}"
            else:
                from techrag.reranker import APIReranker

                rc = c.reranker.__class__(**{**c.reranker.__dict__, **override})
                s = APIReranker(rc).score("PCIe link training", ["The LTSSM controls link training.",
                                                                  "A recipe for banana bread."])
                result["ok"] = s[0] > s[1]
                result["detail"] = f"scores {s[0]:.3f} vs {s[1]:.3f}"
            result["latency_ms"] = int((time.time() - t) * 1000)
        except Exception as exc:
            result.update(ok=False, error=f"{exc.__class__.__name__}: {exc}", code=getattr(exc, "code", "") or "error")
        return result

    # --------------------------------------------------------------- library
    @app.post("/api/library/open", dependencies=guard)
    def library_open(req: LibraryOpen):
        path = Path(req.path).expanduser()
        if req.read_only and not (path / "index.sqlite").exists():
            raise HTTPException(400, "no index.sqlite in that folder")
        return put_settings({"paths": {"library_dir": str(path)}, "read_only": req.read_only})

    @app.post("/api/library/publish", dependencies=guard)
    def library_publish(req: PublishRequest):
        c: Config = state["cfg"]
        dest = Path(req.dest).expanduser()
        if dest.resolve() == c.library_dir.resolve():
            raise HTTPException(400, "destination is the current library")
        target = eng().store.publish(dest, c.sources_dir, c.vlm_cache_dir)
        return {"ok": True, "path": str(target.parent)}

    # ------------------------------------------------------------- documents
    @app.get("/api/documents", dependencies=guard)
    def documents():
        return [d.to_dict() | {"has_toc": bool(d.toc)} for d in eng().store.documents()]

    @app.get("/api/documents/{doc_id}", dependencies=guard)
    def document(doc_id: int):
        e = eng()
        d = e.store.document(doc_id)
        if not d:
            raise HTTPException(404, "document not found")
        return d.to_dict(with_toc=True) | {"page_labels": e.store.page_labels(doc_id)}

    @app.get("/api/documents/{doc_id}/page/{page}/info", dependencies=guard)
    def page_info(doc_id: int, page: int):
        """Physical page index vs printed label, and whether word positions exist (for highlights)."""
        e = eng()
        d = e.store.document(doc_id)
        if not d or not (1 <= page <= max(d.n_pages, 1)):
            raise HTTPException(404, "page not found")
        g = e.store.page_geom(doc_id, page)
        return {"page": page, "n_pages": d.n_pages, "label": g.label if g else "", "has_text": bool(g and g.has_text),
                "geometry": g is not None, "legacy": d.legacy, "sha256": d.sha256}

    @app.get("/api/documents/{doc_id}/page/{page}.png", dependencies=guard)
    def page_image(doc_id: int, page: int, dpi: int = 110):
        e = eng()
        d = e.store.document(doc_id)
        if not d or not (1 <= page <= max(d.n_pages, 1)):
            raise HTTPException(404, "page not found")
        png = e.render_page(doc_id, page, max(60, min(dpi, 220)))
        if png is None:
            raise HTTPException(404, "page cannot be rendered (file missing or not a PDF)")
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/documents/{doc_id}/file", dependencies=guard)
    def document_file(doc_id: int):
        path = eng().document_path(doc_id)
        if not path or not path.exists():
            raise HTTPException(404, "file not found")
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, filename=path.name, content_disposition_type="inline")

    @app.post("/api/documents/{doc_id}/open", dependencies=guard)
    def document_open(doc_id: int):
        """Desktop only: open the original file in the system's default viewer (not a browser tab)."""
        if not desktop:
            raise HTTPException(400, "only available in the desktop app")
        path = eng().document_path(doc_id)
        if not path or not path.exists():
            raise HTTPException(404, "file not found")
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {"ok": True}

    # ---------------------------------------------------------------- search
    @app.post("/api/search", dependencies=guard)
    def search(req: SearchRequest):
        res = eng().retrieve(req.query, domains=req.domains, doc_ids=req.doc_ids, top_k=req.top_k)
        return {"plan": res.plan.to_dict(), "scope": res.scope.to_dict(), "confidence": res.confidence,
                "timings": res.timings, "sources": [p.to_dict() for p in res.passages],
                "parameters": [p.__dict__ for p in res.parameters]}

    @app.post("/api/ask", dependencies=guard)
    def ask(req: AskRequest):
        e = eng()
        history = [t.model_dump() for t in req.history]
        if not req.stream:
            return e.ask(req.question, history, req.domains, req.doc_ids, req.top_k)

        def events():
            try:
                for ev in e.ask_stream(req.question, history, req.domains, req.doc_ids, req.top_k):
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except Exception as exc:
                err = {"type": "error", "message": f"{exc.__class__.__name__}: {exc}"}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------- ingestion
    def start_job(kind: str, target: Optional[Path], rebuild: bool = False) -> dict:
        from techrag.ingest.pipeline import Ingestor

        c: Config = state["cfg"]
        if c.read_only:
            raise HTTPException(403, "the library is opened read-only")
        e = eng()
        job = jobs.create(kind)

        def run():
            with jobs.lock:
                job["status"] = "running"
                try:
                    ing = Ingestor(c, e.store, e.embedder, e.domains, progress=lambda m: job["messages"].append(m),
                                   llm=e.llm if c.llm.model else None, vision=e.vision)
                    report = ing.run(target, rebuild=rebuild, prune=target is None)
                    job["report"] = {"summary": report.summary(), "failed": report.failed,
                                     "warnings": report.warnings}
                    job["status"] = "failed" if report.failed else "done"
                except Exception as exc:
                    job["messages"].append(f"ERROR: {exc}")
                    job["status"] = "failed"

        threading.Thread(target=run, daemon=True).start()
        return job

    def collection_dir(collection: str) -> Path:
        key = re.sub(r"[^\w\-]+", "_", collection.strip().lower()).strip("_") or "general"
        d = state["cfg"].sources_dir / key
        d.mkdir(parents=True, exist_ok=True)
        return d

    @app.post("/api/sources/add", dependencies=guard)
    def sources_add(req: PathsRequest):
        dest_dir = collection_dir(req.collection)
        copied = []
        for p in req.paths:
            src = Path(p)
            if not src.is_file() or src.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            dest = dest_dir / _safe_filename(src.name)
            shutil.copy2(src, dest)
            copied.append(str(dest))
        if not copied:
            raise HTTPException(400, f"no supported files ({', '.join(sorted(SUPPORTED_SUFFIXES))})")
        return {"job": start_job("add", dest_dir)["id"], "copied": copied}

    @app.post("/api/upload", dependencies=guard)
    async def upload(file: UploadFile = File(...), domain: str = Form("general")):
        name = _safe_filename(file.filename or "upload")
        if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise HTTPException(400, f"unsupported file type; allowed: {', '.join(sorted(SUPPORTED_SUFFIXES))}")
        dest = collection_dir(domain) / name
        with open(dest, "wb") as fh:
            while chunk := await file.read(1 << 20):
                fh.write(chunk)
        return {"job": start_job("upload", dest)["id"], "path": str(dest)}

    @app.post("/api/library/migrate", dependencies=guard)
    def library_migrate():
        """Update an index from an older version in place (no re-embedding, no VLM calls)."""
        from techrag.ingest.pipeline import migrate_index

        c: Config = state["cfg"]
        if c.read_only:
            raise HTTPException(403, "the library is opened read-only")
        e = eng()
        job = jobs.create("migrate")

        def run():
            with jobs.lock:
                job["status"] = "running"
                try:
                    rep = migrate_index(c, e.store, lambda m: job["messages"].append(m))
                    job["report"] = rep
                    job["messages"].append(json.dumps(rep, ensure_ascii=False, indent=1))
                    job["status"] = "done"
                except Exception as exc:
                    job["messages"].append(f"ERROR: {exc}")
                    job["status"] = "failed"

        threading.Thread(target=run, daemon=True).start()
        return {"job": job["id"]}

    @app.post("/api/ingest", dependencies=guard)
    def ingest_all(req: IngestRequest = IngestRequest()):
        return {"job": start_job("ingest", None, req.rebuild)["id"]}

    @app.get("/api/jobs/{job_id}", dependencies=guard)
    def job_status(job_id: str):
        job = jobs.items.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return JSONResponse(job)

    return app
