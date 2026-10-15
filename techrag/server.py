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
from techrag.config import Config, ServiceConfig, resource_path, same_path
from techrag.engine import RAGEngine
from techrag.ingest.loaders import SUPPORTED_SUFFIXES
from techrag.ingest.pipeline import IngestStopped, WriteGate
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
    buckets: Optional[list[str]] = None
    doc_ids: Optional[list[int]] = None
    top_k: Optional[int] = Field(default=None, ge=1, le=30)
    stream: bool = True
    clarification: Optional[dict] = None   # the pending clarifying question this message may answer


class RetryRequest(BaseModel):
    paths: Optional[list[str]] = None   # None: everything that needs a retry


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    domains: Optional[list[str]] = None
    buckets: Optional[list[str]] = None
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


class FolderRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4000)


class FeedbackRequest(BaseModel):
    rating: str
    question: str = Field(min_length=1, max_length=4000)
    answer: str = Field(default="", max_length=50000)
    verification_status: str = ""
    sources: list[dict] = Field(default_factory=list)
    correction: dict = Field(default_factory=dict)
    unanswerable: bool = False


class EvalRequest(BaseModel):
    set: str = "bundled"              # bundled | feedback | custom
    yaml: Optional[str] = Field(default=None, max_length=2_000_000)   # the custom set's file content
    name: str = Field(default="", max_length=200)                      # the custom set's file name
    retrieval_only: bool = False


STOPPED_MESSAGE = "stopped: the application is closing (remaining documents are processed by the next rescan)"


class Jobs:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.gate = WriteGate()  # full exit of the desktop app: see shutdown()

    def running(self) -> bool:
        """An indexing job (which writes the library) is queued or running. An evaluation only reads and simply
        stops when the application closes."""
        return any(j["status"] in ("queued", "running") and j["kind"] != "eval" for j in list(self.items.values()))

    def shutdown(self, timeout: float = 60.0) -> bool:
        """The application is closing: jobs stop before their next document, the index write in progress is allowed
        to finish and no write starts afterwards. False if a write did not finish within `timeout` (SQLite still
        rolls an interrupted transaction back, so the index stays consistent)."""
        return self.gate.close(timeout)

    def create(self, kind: str) -> dict:
        if self.gate.stop.is_set():
            raise HTTPException(503, "the application is closing")
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "status": "queued", "messages": [], "report": None,
               "progress": {}, "created": time.time()}
        self.items[job["id"]] = job
        return job


def _same_server(a: str, b: str) -> bool:
    """Same scheme, host and port (a stored API key may only go back to the server it was entered for)."""
    try:
        return bool(a and b) and tls.host_port(normalize_base_url(a)) == tls.host_port(normalize_base_url(b))
    except Exception:
        return False


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

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        """The desktop window keeps the same origin across restarts (stable port), so its HTTP cache outlives an
        update: the page and its scripts must be revalidated (ETag) instead of being reused from the cache."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

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
                "buckets": e.store.buckets() if e else [], "roots": e.store.roots() if e else [],
                "missing_documents": sum(b["missing"] for b in e.store.buckets()) if e else 0,
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

    @app.get("/api/settings/context", dependencies=guard)
    def context_info():
        """The context length the chat model server reports (vLLM max_model_len) next to the setting; the
        smaller of the two is used."""
        e = state["engine"]
        server = None
        if e is not None:
            try:
                server = e.llm.context_window()
            except Exception:
                server = None
        setting = state["cfg"].llm.context_tokens
        return {"setting": setting, "server": server, "used": min(setting, server) if server else setting}

    def _probe_service(p: ServiceProbe) -> ServiceConfig:
        if p.service not in SERVICES:
            raise HTTPException(400, f"unknown service '{p.service}'")
        c: Config = state["cfg"]
        saved = c.vision_service() if p.service == "vision" else getattr(c, p.service)
        url = p.base_url if p.base_url is not None else saved.base_url
        # The stored key is only reused for the server it belongs to - never sent to another URL given in the
        # request. The vision service shows "" when it uses the chat model's key (as at run time).
        reuse = p.api_key in (None, MASK) or (p.service == "vision" and p.api_key == "")
        key = (saved.api_key if _same_server(url, saved.base_url) else "") if reuse else p.api_key
        return ServiceConfig(base_url=url, api_key=key or "", model=p.model if p.model is not None else saved.model,
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
        store = eng().store
        try:
            target = store.publish(dest, c.sources_dir, c.vlm_cache_dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "path": str(target.parent), "report": getattr(store, "last_publish", {})}

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
    def page_image(request: Request, doc_id: int, page: int, dpi: int = 110):
        e = eng()
        d = e.store.document(doc_id)
        if not d or not (1 <= page <= max(d.n_pages, 1)):
            raise HTTPException(404, "page not found")
        dpi = max(60, min(dpi, 220))
        # Document ids can be reused (a deleted document's id goes to the next one), so the cache key is the
        # content: a cached image is only reused after the browser revalidated it against the document's hash.
        headers = {"Cache-Control": "private, no-cache", "ETag": f'"{d.sha256[:24]}-{page}-{dpi}"'}
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        png = e.render_page(doc_id, page, dpi)
        if png is None:
            raise HTTPException(404, "page cannot be rendered (file missing or not a PDF)")
        return Response(png, media_type="image/png", headers=headers)

    @app.get("/api/documents/{doc_id}/file", dependencies=guard)
    def document_file(doc_id: int):
        path = document_source(doc_id)
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in (".html", ".htm"):  # never rendered on this origin (it could read the token)
            return FileResponse(path, media_type=media, filename=path.name, content_disposition_type="attachment",
                                headers={"Content-Security-Policy": "sandbox"})
        return FileResponse(path, media_type=media, filename=path.name, content_disposition_type="inline")

    def document_source(doc_id: int) -> Path:
        """The original file of an indexed document - only a document type the app indexes (an index from a shared
        drive could have been edited to point at a program)."""
        path = eng().document_path(doc_id)
        if not path or not path.is_file():
            raise HTTPException(404, "file not found")
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise HTTPException(403, "not a document type this app indexes")
        return path

    @app.post("/api/documents/{doc_id}/open", dependencies=guard)
    def document_open(doc_id: int):
        """Desktop only: open the original file in the system's default viewer (not a browser tab)."""
        if not desktop:
            raise HTTPException(400, "only available in the desktop app")
        path = document_source(doc_id)
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        return {"ok": True}

    # ---------------------------------------------------------------- search
    @app.post("/api/search", dependencies=guard)
    def search(req: SearchRequest):
        res = eng().retrieve(req.query, domains=req.domains, doc_ids=req.doc_ids, top_k=req.top_k, buckets=req.buckets)
        return {"plan": res.plan.to_dict(), "scope": res.scope.to_dict(), "confidence": res.confidence,
                "timings": res.timings, "sources": [p.to_dict() for p in res.passages],
                "parameters": [p.__dict__ for p in res.parameters]}

    # ---------------------------------------------------------- question history
    def history_store():
        from techrag.history import HistoryStore

        return HistoryStore(state["cfg"].user_data_dir)

    def keep_answer(question: str, final: dict) -> None:
        """The answer shown goes to the question history (Evaluation window). Never breaks the answer."""
        if not state["cfg"].answer.keep_history:
            return
        try:
            from techrag.history import record_from_final

            history_store().add(record_from_final(question, final))
        except Exception:
            pass

    @app.get("/api/history", dependencies=guard)
    def list_history(q: str = "", limit: int = 100, offset: int = 0):
        items, total = history_store().search(q, max(1, min(limit, 500)), max(0, offset))
        return {"items": items, "total": total, "enabled": state["cfg"].answer.keep_history}

    @app.delete("/api/history/{hid}", dependencies=guard)
    def delete_history(hid: str):
        if not history_store().delete(hid):
            raise HTTPException(404, "history entry not found")
        return {"ok": True}

    @app.delete("/api/history", dependencies=guard)
    def clear_history():
        return {"ok": True, "deleted": history_store().clear()}

    @app.post("/api/ask", dependencies=guard)
    def ask(req: AskRequest):
        e = eng()
        history = [t.model_dump() for t in req.history]
        # The non-streaming form serves scripts; only the chat (streaming) is kept in the question history.
        if not req.stream:
            return e.ask(req.question, history, req.domains, req.doc_ids, req.top_k, req.buckets, req.clarification)

        def events():
            try:
                for ev in e.ask_stream(req.question, history, req.domains, req.doc_ids, req.top_k, req.buckets,
                                       req.clarification):
                    if ev.get("type") == "final":
                        keep_answer(req.question, ev)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except Exception as exc:
                err = {"type": "error", "message": f"{exc.__class__.__name__}: {exc}"}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------- ingestion
    def start_job(kind: str, target: Optional[Path] = None, rebuild: bool = False, root_id: Optional[int] = None,
                  everything: bool = False, retry: bool = False, retry_paths: Optional[list[str]] = None) -> dict:
        """Indexing runs in a background thread (the window stays responsive); progress is polled."""
        from techrag.ingest.pipeline import Ingestor

        c: Config = state["cfg"]
        if c.read_only:
            raise HTTPException(403, "the library is opened read-only")
        e = eng()
        job = jobs.create(kind)

        def status(st: dict) -> None:
            job["progress"] = dict(job["progress"], **st)

        def run():
            with jobs.lock:
                job["status"] = "running"
                ing = None
                try:
                    jobs.gate.check()
                    ing = Ingestor(c, e.store, e.embedder, e.domains, progress=lambda m: job["messages"].append(m),
                                   llm=e.llm if c.llm.model else None, vision=e.vision, status=status,
                                   gate=jobs.gate)
                    ing.run_kind = kind
                    if retry:
                        report = ing.retry_failed(retry_paths)
                    elif root_id is not None:
                        report = ing.sync_root(root_id, rebuild)
                    elif everything:
                        report = ing.sync_all(rebuild)
                    else:
                        report = ing.run(target, rebuild=rebuild, prune=target is None)
                    job["report"] = report.to_dict()
                    job["status"] = "failed" if report.failed and not (report.added or report.updated) else "done"
                except IngestStopped:
                    job["messages"].append(STOPPED_MESSAGE)
                    job["status"] = "cancelled"
                except Exception as exc:
                    job["messages"].append(f"ERROR: {exc}")
                    job["status"] = "failed"
                    if ing is not None:
                        ing.abort_run("failed")

        threading.Thread(target=run, daemon=True).start()
        return job

    def _plan(root_path: str, root_id: Optional[int]) -> dict:
        from techrag.ingest.pipeline import ingest_fingerprint
        from techrag.ingest.sources import plan_changes, plan_summary, scan_root

        e = eng()
        lib = state["cfg"].library_dir
        scan = scan_root(root_path, exclude=(lib,) if Path(root_path) != state["cfg"].sources_dir else ())
        fp = ingest_fingerprint(state["cfg"], e.vision.model if e.vision else None)
        docs = e.store.root_documents(root_id) if root_id is not None or Path(root_path) == state["cfg"].sources_dir \
            else []
        if root_id is None:
            docs = [d for d in docs if not Path(d.path).is_absolute()]
        return {"scan": scan.summary(), "plan": plan_summary(plan_changes(scan, docs, fp, root_id))}

    @app.post("/api/folders/preview", dependencies=guard)
    def folder_preview(req: FolderRequest):
        """What a document folder contains (buckets, documents, unsupported files) and what indexing would do.
        Only reads the folder."""
        path = Path(req.path).expanduser()
        e = eng()
        known = next((r for r in e.store.roots() if same_path(r["path"], path)), None)
        out = _plan(str(path), known["id"] if known else None)
        out["known"] = bool(known)
        return out

    @app.post("/api/folders", dependencies=guard)
    def folder_add(req: FolderRequest):
        """Register a document folder (read in place, nothing copied or written there) and index it."""
        c: Config = state["cfg"]
        if c.read_only:
            raise HTTPException(403, "the library is opened read-only")
        path = Path(req.path).expanduser()
        if not path.is_dir():
            raise HTTPException(400, "folder not found")
        try:
            if path.resolve() == c.library_dir.resolve() or c.library_dir.resolve() in path.resolve().parents:
                raise HTTPException(400, "pick a folder outside the library folder")
        except OSError:
            pass
        store = eng().store
        # "c:\docs" and "C:\Docs" are the same folder on Windows: never register (and index) it twice.
        known = next((r for r in store.roots() if same_path(r["path"], path)), None)
        root_id = known["id"] if known else store.add_root(str(path))
        return {"root_id": root_id, "job": start_job("folder", root_id=root_id)["id"]}

    @app.get("/api/folders", dependencies=guard)
    def folders():
        return eng().store.roots()

    @app.post("/api/rescan/preview", dependencies=guard)
    def rescan_preview():
        c: Config = state["cfg"]
        e = eng()
        parts = [dict(_plan(str(c.sources_dir), None), root=str(c.sources_dir), library=True)]
        for r in e.store.roots():
            parts.append(dict(_plan(r["path"], r["id"]), root=r["path"], library=False))
        total: dict = {}
        for p in parts:
            for k, v in p["plan"]["counts"].items():
                total[k] = total.get(k, 0) + v
        return {"roots": parts, "counts": total}

    @app.post("/api/rescan", dependencies=guard)
    def rescan(req: IngestRequest = IngestRequest()):
        return {"job": start_job("rescan", rebuild=req.rebuild, everything=True)["id"]}

    # ------------------------------------------------------------ indexing quality
    @app.get("/api/quality", dependencies=guard)
    def quality():
        """The indexing quality report: the last runs and, per document, what its last processing produced.
        Documents indexed before this report existed have no record ("not available"); nothing is guessed.
        The report holds no server address, key or token."""
        from techrag.ingest.pipeline import retry_plan

        e = eng()
        store = e.store
        records = store.quality()
        docs = store.documents()
        out = []
        seen = set()
        for d in docs:
            seen.add(d.path)
            out.append({"doc_id": d.id, "path": d.path, "title": d.title, "bucket": d.bucket,
                        "rel_path": d.rel_path or d.path, "pdf": (d.rel_path or d.path).lower().endswith(".pdf"),
                        "missing": d.missing, "ingested_at": d.ingested_at, "n_pages": d.n_pages,
                        "quality": (records or {}).get(d.path)})
        for path, rec in (records or {}).items():
            if path in seen:
                continue  # never indexed successfully: only the failure record exists
            data = rec.get("data") or {}
            src = data.get("source") or {}
            out.append({"doc_id": None, "path": path, "title": data.get("title") or Path(path).stem,
                        "bucket": src.get("bucket", ""), "rel_path": src.get("rel_path") or path,
                        "pdf": str(src.get("rel_path") or path).lower().endswith(".pdf"), "missing": False,
                        "ingested_at": "", "n_pages": 0, "quality": rec})
        c: Config = state["cfg"]
        return {"available": records is not None, "runs": store.ingest_runs(10), "documents": out,
                "retry": retry_plan(store) if records is not None else [],
                "vlm_enabled": bool(c.vision.enabled and c.vision_service().model),
                "read_only": c.read_only}

    @app.post("/api/quality/retry", dependencies=guard)
    def quality_retry(req: RetryRequest = RetryRequest()):
        """Retry what failed (only the failed VLM pages where possible; see Ingestor.retry_failed)."""
        return {"job": start_job("retry", retry=True, retry_paths=req.paths)["id"]}

    @app.post("/api/documents/purge-missing", dependencies=guard)
    def purge_missing():
        """Remove documents whose source file is gone from the index (explicit user action)."""
        if state["cfg"].read_only:
            raise HTTPException(403, "the library is opened read-only")
        return {"removed": eng().store.purge_missing()}

    def collection_dir(collection: str) -> Path:
        """Folder of a collection inside the library's sources: the name as typed (Turkish letters and spaces
        kept), only characters Windows forbids in file names are replaced."""
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", collection.strip()).strip(" .") or "general"
        d = state["cfg"].sources_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def unique_dest(dest_dir: Path, name: str, src: Optional[Path] = None) -> Path:
        """Never overwrite a different file with the same name: 'Spec.pdf' -> 'Spec (2).pdf'."""
        from techrag.ingest.pipeline import sha256_file

        dest = dest_dir / name
        n = 2
        while dest.exists():
            if src is not None and src.is_file() and sha256_file(dest) == sha256_file(src):
                return dest  # the same file again
            dest = dest_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        return dest

    def check_can_add() -> None:
        """Before anything is copied or written into the library's sources folder."""
        c: Config = state["cfg"]
        if c.read_only:
            raise HTTPException(403, "the library is opened read-only")
        if not c.server.allow_upload:
            raise HTTPException(403, "adding documents is switched off on this server (server.allow_upload)")
        eng()

    @app.post("/api/sources/add", dependencies=guard)
    def sources_add(req: PathsRequest):
        check_can_add()
        dest_dir = collection_dir(req.collection)
        copied = []
        for p in req.paths:
            src = Path(p)
            if not src.is_file() or src.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            dest = unique_dest(dest_dir, _safe_filename(src.name), src)
            if not dest.exists():
                shutil.copy2(src, dest)
            copied.append(str(dest))
        if not copied:
            raise HTTPException(400, f"no supported files ({', '.join(sorted(SUPPORTED_SUFFIXES))})")
        return {"job": start_job("add", dest_dir)["id"], "copied": copied}

    @app.post("/api/upload", dependencies=guard)
    async def upload(file: UploadFile = File(...), domain: str = Form("general")):
        check_can_add()
        name = _safe_filename(file.filename or "upload")
        if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise HTTPException(400, f"unsupported file type; allowed: {', '.join(sorted(SUPPORTED_SUFFIXES))}")
        dest = unique_dest(collection_dir(domain), name)
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
                    rep = migrate_index(c, e.store, lambda m: job["messages"].append(m), gate=jobs.gate)
                    job["report"] = rep
                    job["messages"].append(json.dumps(rep, ensure_ascii=False, indent=1))
                    job["status"] = "done"
                except IngestStopped:
                    job["messages"].append(STOPPED_MESSAGE)
                    job["status"] = "cancelled"
                except Exception as exc:
                    job["messages"].append(f"ERROR: {exc}")
                    job["status"] = "failed"

        threading.Thread(target=run, daemon=True).start()
        return {"job": job["id"]}

    # -------------------------------------------------------------- feedback
    def feedback_store():
        from techrag.feedback import FeedbackStore

        return FeedbackStore(state["cfg"].user_data_dir)

    @app.post("/api/feedback", dependencies=guard)
    def add_feedback(req: FeedbackRequest):
        try:
            rec = feedback_store().add(req.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(500, f"feedback could not be saved: {exc.__class__.__name__}")
        return {"ok": True, "id": rec["id"], "summary": feedback_store().summary()}

    @app.get("/api/feedback", dependencies=guard)
    def list_feedback():
        fs = feedback_store()
        return {"items": list(reversed(fs.items()))[:500], "summary": fs.summary()}

    @app.delete("/api/feedback/{fid}", dependencies=guard)
    def delete_feedback(fid: str):
        if not feedback_store().delete(fid):
            raise HTTPException(404, "feedback not found")
        return {"ok": True, "summary": feedback_store().summary()}

    @app.get("/api/feedback/export", dependencies=guard)
    def export_feedback():
        from techrag.feedback import export_yaml

        return Response(export_yaml(feedback_store().items()), media_type="text/yaml; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="feedback_candidates.yaml"'})

    # ------------------------------------------------------------ evaluation
    def eval_dir() -> Path:
        return state["cfg"].user_data_dir / "eval_reports"

    @app.get("/api/eval", dependencies=guard)
    def eval_info():
        from techrag.evaluation import bundled_questions, list_reports, load_items
        from techrag.feedback import candidate_items

        try:
            bundled = len(load_items(bundled_questions()))
        except Exception:
            bundled = 0
        running = next((j["id"] for j in jobs.items.values()
                        if j["kind"] == "eval" and j["status"] in ("queued", "running")), None)
        return {"sets": {"bundled": bundled, "feedback": len(candidate_items(feedback_store().items()))},
                "reports": list_reports(eval_dir()), "running": running}

    @app.get("/api/eval/questions", dependencies=guard)
    def eval_questions():
        """The bundled example question set, to check and complete it against the library and load it back as an
        own question file."""
        from techrag.evaluation import bundled_questions

        return Response(bundled_questions().read_text(encoding="utf-8"), media_type="text/yaml; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="questions.yaml"'})

    @app.get("/api/eval/reports/{name}", dependencies=guard)
    def eval_report(name: str):
        from techrag.evaluation import read_report

        rep = read_report(eval_dir(), name)
        if rep is None:
            raise HTTPException(404, "report not found")
        return rep

    @app.post("/api/eval/run", dependencies=guard)
    def eval_run(req: EvalRequest):
        """Runs a question set against the current library in the background (one evaluation at a time). The
        report is saved in the user data folder; nothing is written into the library."""
        from techrag.evaluation import (EvalStopped, bundled_questions, load_items, load_items_text, parse_items,
                                        run_eval, save_report)
        from techrag.feedback import candidate_items
        from techrag.ingest.quality import scrub

        if any(j["kind"] == "eval" and j["status"] in ("queued", "running") for j in jobs.items.values()):
            raise HTTPException(409, "an evaluation is already running")
        try:
            if req.set == "bundled":
                items, label = load_items(bundled_questions()), "bundled"
            elif req.set == "feedback":
                items, label = parse_items(candidate_items(feedback_store().items())), "feedback"
            elif req.set == "custom":
                items, label = load_items_text(req.yaml or ""), Path(req.name or "custom.yaml").name
            else:
                raise ValueError(f"unknown question set '{req.set}'")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not items:
            raise HTTPException(400, "the question set is empty")
        e = eng()
        job = jobs.create("eval")
        job["cancel"] = False
        job["progress"] = {"done": 0, "total": len(items)}

        def run():
            job["status"] = "running"
            try:
                rep = run_eval(e, items, retrieval_only=req.retrieval_only,
                               progress=lambda m: job["messages"].append(m),
                               should_stop=lambda: job["cancel"] or jobs.gate.stop.is_set(),
                               on_item=lambda n, total: job["progress"].update(done=n, total=total))
                rep.update(set=label, retrieval_only=req.retrieval_only)
                path = save_report(rep, eval_dir())
                job["report"] = {"name": path.name, "summary": rep["summary"], "set": label}
                job["status"] = "done"
            except EvalStopped as exc:
                job["messages"].append(str(exc))
                job["status"] = "cancelled"
            except Exception as exc:
                job["messages"].append(f"ERROR: {scrub(f'{exc.__class__.__name__}: {exc}')}")
                job["status"] = "failed"

        threading.Thread(target=run, daemon=True).start()
        return {"job": job["id"], "total": len(items)}

    @app.post("/api/jobs/{job_id}/cancel", dependencies=guard)
    def cancel_job(job_id: str):
        job = jobs.items.get(job_id)
        if not job or job["kind"] != "eval":
            raise HTTPException(404, "no evaluation with this id")
        job["cancel"] = True
        return {"ok": True}

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
