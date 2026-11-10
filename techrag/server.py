"""FastAPI backend for the desktop window (and optional browser use). Serves the offline UI and the API."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from techrag import __version__, askctx, mirror, org, tls
from techrag.api import APIClient, check_service, configure_tls, current_tls, error_code, normalize_base_url
from techrag.config import PERSONAL_INDEX_DIR, Config, ServiceConfig, _apply_env, apply_dict, resource_path, same_path
from techrag.engine import RAGEngine
from techrag.store import Store
from techrag.ingest.loaders import SUPPORTED_SUFFIXES
from techrag.ingest.pipeline import IngestStopped, WriteGate
from techrag.settings import MASK, load_user_settings, public_settings, settings_path, update_settings
from techrag.usage import UsageLog, current_machine, current_user, token_totals

log = logging.getLogger("techrag.server")

WEB_DIR = resource_path("web")
SERVICES = ("llm", "vision", "embedding", "reranker")
PROBE_SERVICES = SERVICES + ("llm_fast",)   # the fast chat model's card is tested like the chat model's


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
    mode: str = ""                          # which chat model answers: "fast" (Hızlı) | "deep"/"" (Kapsamlı)


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


class OrgSaveRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4000)
    others_read_only: bool = True


class OrgUseRequest(BaseModel):
    path: str = Field(default="", max_length=4000)


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
    mode: str = ""                    # which chat model answers: "fast" | "deep"/""


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


def ui_theme(cfg: Config) -> str:
    """The page theme: dark (the default) unless the user chose light."""
    return "light" if getattr(cfg.ui, "theme", "dark") == "light" else "dark"


def create_app(cfg: Config, engine: Optional[RAGEngine] = None, warmup: bool = True,
               desktop: bool = False) -> FastAPI:
    jobs = Jobs()
    state = {"cfg": cfg, "engine": engine, "error": None}
    engine_lock = threading.Lock()

    def mirror_ready(path: Path) -> None:
        """A new local copy of the (read-only, network) library's index: answer from it from now on."""
        e = state["engine"]
        if e is None or not mirror.wanted(state["cfg"]):
            return
        try:
            e.replace_shared(Store(path, read_only=True))
        except Exception as exc:
            mirrors.mirror.status.update(state="error", error=f"{exc.__class__.__name__}: {exc}"[:300])

    mirrors = mirror.MirrorManager(mirror_ready)

    def build_engine(c: Config) -> None:
        try:
            local = mirrors.configure(c)
            state["engine"] = RAGEngine(c, store=Store(local, read_only=True) if local else None)
            state["error"] = None
        except Exception as exc:
            state["engine"] = None
            state["error"] = f"{exc.__class__.__name__}: {exc}"

    if engine is None:
        build_engine(cfg)
    else:
        mirrors.configure(cfg)

    def eng() -> RAGEngine:
        e = state["engine"]
        if e is None:
            raise HTTPException(503, state["error"] or "library not available")
        return e

    def warm():
        e = state["engine"]
        if not e:
            return
        if mirrors.mirror is not None and mirrors.mirror.current() is None:
            return   # the first local copy is being taken: its vectors are loaded from it, not over the network
        try:
            with e.activity.running():   # a copy switch never overlaps it (one set of vectors at a time)
                e.store.vectors()
        except MemoryError as exc:   # the first question tries again and says so if it cannot
            log.warning("search vectors not loaded at start: %s", exc)
        except Exception as exc:
            state["error"] = str(exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if warmup:
            threading.Thread(target=warm, daemon=True).start()
            mirrors.start()
        yield
        mirrors.stop()

    # Swagger UI pulls assets from a CDN, which does not exist in an air-gapped network.
    app = FastAPI(title="techrag", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.jobs = jobs
    app.state.state = state
    app.state.mirrors = mirrors
    # desktop: called with {"kind": final|clarify|error, "question", "status"} when an answer is ready (the app
    # tells the user when its window is minimized, hidden or behind other windows)
    app.state.on_answer = None
    asks: dict[str, askctx.AskContext] = {}   # answers in progress, by the id their stream starts with
    app.state.asks = asks

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
        # The page starts in the user's theme (dark unless they chose light): set before anything is drawn, so the
        # window never flashes the other theme while the script loads.
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace('data-theme="dark"', f'data-theme="{ui_theme(state["cfg"])}"', 1))

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
                "read_only": c.read_only, "language": c.ui.language, "theme": ui_theme(c),
                "legacy_documents": stats.get("legacy_documents", 0),
                "buckets": e.store.buckets() if e else [], "roots": e.store.roots() if e else [],
                "missing_documents": sum(b["missing"] for b in e.store.buckets()) if e else 0,
                "models": {**{s: getattr(c, s).model for s in SERVICES}, "llm_fast": c.llm_fast.model},
                "stats": stats, "domains": domains, "error": state["error"],
                "allow_upload": c.server.allow_upload and not c.read_only,
                "personal": personal_info(), "org": org.public_status(c),
                "mirror": dict(mirrors.status(), read_only=c.read_only)}

    @app.post("/api/library/mirror/sync", dependencies=guard)
    def mirror_sync():
        """Look at the network library now (it is otherwise looked at when the app starts) and copy it when it
        changed."""
        if mirrors.mirror is None:
            raise HTTPException(400, "no local copy is used for this library")
        if mirrors._thread is not None and mirrors._thread.is_alive():
            mirrors.sync_now()
        else:
            threading.Thread(target=mirrors.check, daemon=True).start()
        return {"ok": True, "mirror": mirrors.status()}

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
            if set(patch) - {"ui", "usage"} or state["engine"] is None:   # these two need no new engine
                build_engine(new_cfg)
            else:
                state["engine"].cfg = new_cfg
        return {"settings": public_settings(new_cfg), "error": state["error"]}

    # ------------------------------------------------------------ organization settings
    def _apply_org(values: dict) -> None:
        """Rebuild the running configuration with new organization settings: defaults < organization < the user's
        own settings < environment (as at start-up)."""
        from techrag.settings import follow_org

        new_cfg = state["cfg"].copy()
        apply_dict(new_cfg, values, strict=False)
        apply_dict(new_cfg, follow_org(load_user_settings()), strict=False)
        _apply_env(new_cfg)
        new_cfg.share_api_key(adopt_legacy=False)
        new_cfg.server = state["cfg"].server
        state["cfg"] = new_cfg
        configure_tls(new_cfg.tls)
        build_engine(new_cfg)

    @app.post("/api/org/save", dependencies=guard)
    def org_save(req: OrgSaveRequest):
        """Admin: write the current settings as the organization's (no API key, no personal folders) into the given
        folder, and the pointer file next to the exe that is handed out with it."""
        with engine_lock:
            try:
                out = org.save(req.path, state["cfg"], req.others_read_only)
            except FileNotFoundError:
                raise HTTPException(400, "folder not found")
            except OSError as exc:
                raise HTTPException(400, f"the file could not be written: {exc.__class__.__name__}")
            # "others open it read-only" is for the others: the admin's own way of opening it is kept as their own
            update_settings(state["cfg"], {"org": {"path": str(Path(out["file"]).parent), "asked": True},
                                           "read_only": state["cfg"].read_only})
            state["cfg"].org.path, state["cfg"].org.asked = str(Path(out["file"]).parent), True
        return out

    @app.post("/api/org/use", dependencies=guard)
    def org_use(req: OrgUseRequest):
        """First start: the folder holding the organization settings (asked once), or "" when there are none."""
        with engine_lock:
            if not req.path.strip():
                state["cfg"] = update_settings(state["cfg"], {"org": {"asked": True}})
                return {"ok": True, "org": org.public_status(state["cfg"])}
            target = org.org_file(req.path)
            if not target.is_file():
                raise HTTPException(400, "no organization settings file in this folder")
            user = load_user_settings()
            user["org"] = {"path": str(target.parent), "asked": True}
            values = org.load(user)
            if not org.STATUS["loaded"]:
                raise HTTPException(400, "the organization settings file cannot be read")
            update_settings(state["cfg"], {"org": {"path": str(target.parent), "asked": True}})
            _apply_org(values)
        return {"ok": True, "org": org.public_status(state["cfg"]), "error": state["error"]}

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
        if p.service not in PROBE_SERVICES:
            raise HTTPException(400, f"unknown service '{p.service}'")
        c: Config = state["cfg"]
        saved = c.vision_service() if p.service == "vision" else (
            c.chat_config("fast") if p.service == "llm_fast" else getattr(c, p.service))
        url = p.base_url if p.base_url is not None else saved.base_url
        if p.service == "llm_fast" and not (url or "").strip():
            url = c.llm.base_url   # no server of its own: the chat model's
        # The shared key is only reused for the model servers it is configured for - never sent to another URL
        # given in the request.
        configured = (c.llm.base_url, c.chat_config("fast").base_url, c.vision_service().base_url,
                      c.embedding.base_url, c.reranker.base_url)
        reuse = p.api_key in (None, MASK)
        key = (c.auth.api_key if any(_same_server(url, b) for b in configured) else "") if reuse else p.api_key
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
            if p.service in ("llm", "llm_fast", "vision"):
                from techrag.llm import LLMClient

                client = LLMClient(c.chat_config("fast") if p.service == "llm_fast" else c.llm, service=svc)
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
        store = eng().shared
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

    def usage_log() -> Optional[UsageLog]:
        c: Config = state["cfg"]
        if not c.usage.enabled:
            return None
        log_ = state.get("usage")
        if log_ is None or log_.dir != c.usage_log_dir:
            log_ = state["usage"] = UsageLog(c.usage_log_dir, c.user_data_dir / "usage_local")
        return log_

    def record_usage(question: str, ctx: askctx.AskContext, last: dict, stopped: bool, model: str = "") -> None:
        """One line in the user's usage log: how the question ended and the tokens it took (no answer text).
        stopped: the user stopped it (the stream closing after a finished answer also sets ctx.stopped)."""
        log_ = usage_log()
        if log_ is None:
            return
        v = (last or {}).get("verification") or {}
        if stopped:
            outcome = "durduruldu"
        elif not last or last.get("type") == "error":
            outcome = "hata"
        elif v.get("status") == "clarify":
            outcome = "soru_soruldu"
        else:
            outcome = "cevaplandi"
        log_.record(question, outcome, v.get("status", ""), time.time() - ctx.started,
                    token_totals(ctx.summary()), model or state["cfg"].llm.model)

    @app.post("/api/ask", dependencies=guard)
    def ask(req: AskRequest):
        e = eng()
        history = [t.model_dump() for t in req.history]
        model = state["cfg"].chat_config(req.mode).model
        if not req.stream:
            ctx = askctx.AskContext()
            res: dict = {}
            try:
                with askctx.active(ctx):
                    res = e.for_mode(req.mode).ask(req.question, history, req.domains, req.doc_ids, req.top_k,
                                                   req.buckets, req.clarification)
                return res
            finally:
                record_usage(req.question, ctx, dict(res, type="final") if res else {}, ctx.stopped, model)

        ctx = askctx.AskContext()
        ask_id = uuid.uuid4().hex
        asks[ask_id] = ctx
        q: queue.Queue = queue.Queue()

        def work() -> None:
            """The answer runs in a thread of its own: the response only relays its events. Stop (or the page going
            away) stops it - also the model request in progress."""
            last: dict = {}
            stopped = False
            with askctx.active(ctx):
                try:
                    answering = e.for_mode(req.mode)
                    for ev in answering.ask_stream(req.question, history, req.domains, req.doc_ids, req.top_k,
                                                   req.buckets, req.clarification):
                        if ev.get("type") == "final":
                            last = ev
                        q.put(ev)
                except askctx.Cancelled:
                    stopped = True
                    q.put({"type": "cancelled"})
                    return
                except Exception as exc:
                    # code: what kind of failure (memory, connection, certificate ...) - the page words it for the
                    # user and keeps the technical message as detail
                    last = {"type": "error", "message": f"{exc.__class__.__name__}: {exc}", "code": error_code(exc)}
                    q.put(last)
                finally:
                    # Read before the stream ends: closing the stream after the last event also sets ctx.stopped,
                    # which does not mean the user stopped the answer.
                    stopped = stopped or ctx.stopped
                    q.put(None)
                    asks.pop(ask_id, None)
                    try:
                        record_usage(req.question, ctx, last, stopped, model)
                    except Exception:  # the log never breaks an answer
                        pass
            hook = getattr(app.state, "on_answer", None)
            if hook is not None and last and not stopped:
                v = last.get("verification") or {}
                kind = "error" if last.get("type") == "error" else ("clarify" if v.get("status") == "clarify"
                                                                      else "final")
                try:
                    hook({"kind": kind, "question": req.question, "status": v.get("status", "")})
                except Exception:
                    pass

        def events():
            yield f"data: {json.dumps({'type': 'started', 'id': ask_id})}\n\n"
            try:
                while True:
                    try:
                        ev = q.get(timeout=2.0)
                    except queue.Empty:
                        yield ": waiting\n\n"   # keeps the connection alive and lets a closed page be noticed
                        continue
                    if ev is None:
                        return
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            finally:
                ctx.stop()   # the page went away (or the answer is complete: then this changes nothing)

        threading.Thread(target=work, name="answer", daemon=True).start()
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/ask/{ask_id}/stop", dependencies=guard)
    def ask_stop(ask_id: str):
        """Stop an answer in progress: the model request is closed (the server stops generating for it)."""
        ctx = asks.get(ask_id)
        if ctx is None:
            return {"ok": False, "reason": "not running"}
        ctx.stop()
        return {"ok": True}

    # ------------------------------------------------------------- ingestion
    def start_job(kind: str, target: Optional[Path] = None, rebuild: bool = False, root_id: Optional[int] = None,
                  everything: bool = False, retry: bool = False, retry_paths: Optional[list[str]] = None,
                  personal: bool = False) -> dict:
        """Indexing runs in a background thread (the window stays responsive); progress is polled.
        personal: index the user's personal folder (into the personal library; allowed also when the shared library
        is opened read-only)."""
        from techrag.ingest.pipeline import Ingestor

        c: Config = state["cfg"]
        if c.read_only and not personal:
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
                    lib_cfg, store, own_root = c, e.shared, None
                    if personal:
                        (store, own_root), lib_cfg = e.ensure_personal(), c.personal_config()
                    ing = Ingestor(lib_cfg, store, e.embedder, e.domains, progress=lambda m: job["messages"].append(m),
                                   llm=e.llm if c.llm.model else None, vision=e.vision, status=status,
                                   gate=jobs.gate)
                    ing.run_kind = kind
                    if personal and root_id is not None:
                        report = ing.sync_root(root_id, rebuild)
                    elif personal:   # the personal folder and every folder linked to it
                        report = ing.sync_roots([own_root] + [r["id"] for r in store.roots() if r["id"] != own_root],
                                                rebuild)
                    elif retry:
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
        own = root_id is not None or Path(root_path) == state["cfg"].sources_dir
        scan = scan_root(root_path, exclude=(lib,) if Path(root_path) != state["cfg"].sources_dir else (),
                         skip_rels=e.shared.excluded_rels(root_id) if own else frozenset())
        fp = ingest_fingerprint(state["cfg"], e.vision.model if e.vision else None)
        docs = e.shared.root_documents(root_id) if root_id is not None or Path(root_path) == state["cfg"].sources_dir \
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
        known = next((r for r in e.shared.roots() if same_path(r["path"], path)), None)
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
        store = eng().shared
        # "c:\docs" and "C:\Docs" are the same folder on Windows: never register (and index) it twice.
        known = next((r for r in store.roots() if same_path(r["path"], path)), None)
        root_id = known["id"] if known else store.add_root(str(path))
        return {"root_id": root_id, "job": start_job("folder", root_id=root_id)["id"]}

    @app.get("/api/folders", dependencies=guard)
    def folders():
        return eng().shared.roots()

    @app.post("/api/rescan/preview", dependencies=guard)
    def rescan_preview():
        c: Config = state["cfg"]
        e = eng()
        parts = [dict(_plan(str(c.sources_dir), None), root=str(c.sources_dir), library=True)]
        for r in e.shared.roots():
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
        store = e.shared
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
        return {"removed": eng().shared.purge_missing()}

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
        readd(copied)
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
        readd([str(dest)])
        return {"job": start_job("upload", dest)["id"], "path": str(dest)}

    def readd(paths: list[str]) -> None:
        """A file added again on purpose is no longer kept out of the index (it was removed before)."""
        store = eng().shared
        for p in paths:
            try:
                store.restore_excluded(Path(p).resolve().relative_to(state["cfg"].sources_dir.resolve()).as_posix())
            except ValueError:
                pass

    # -------------------------------------------------- removing shared library documents (Admin)
    @app.delete("/api/library/documents/{doc_id}", dependencies=guard)
    def library_remove(doc_id: int):
        """Take a document out of the shared library's index. Its file is not touched (in a document folder or in
        the library's sources folder alike) and later rescans leave it out, until it is restored."""
        from techrag.library import is_personal

        if state["cfg"].read_only:
            raise HTTPException(403, "the library is opened read-only")
        e = eng()
        if is_personal(doc_id) or e.shared.document(doc_id) is None:
            raise HTTPException(404, "not a shared library document")
        if jobs.running():
            raise HTTPException(409, "indexing is running; remove the document when it has finished")
        entry = e.shared.exclude_document(doc_id)
        return {"ok": True, "title": entry["title"], "path": entry["rel_path"]}

    @app.get("/api/library/removed", dependencies=guard)
    def library_removed():
        return eng().shared.excluded()

    @app.post("/api/library/removed/restore", dependencies=guard)
    def library_restore(req: FolderRequest):
        """Bring a removed document back: it is indexed again by a scan of its folder (started here)."""
        if state["cfg"].read_only:
            raise HTTPException(403, "the library is opened read-only")
        e = eng()
        entry = e.shared.restore_excluded(req.path)
        if entry is None:
            raise HTTPException(404, "not a removed document")
        if entry["root_id"] is not None and e.shared.root(entry["root_id"]) is not None:
            job = start_job("restore", root_id=entry["root_id"])
        elif Path(entry["path"]).is_absolute():
            job = start_job("restore", Path(entry["path"]))
        else:
            job = start_job("restore")
        return {"ok": True, "job": job["id"], "title": entry["title"]}

    # ------------------------------------------------------------------ chat history
    def chat_store():
        from techrag.chats import ChatStore

        return ChatStore(state["cfg"].user_data_dir / "chats")

    def chat_id(cid: str) -> str:
        from techrag.chats import valid_id

        if not valid_id(cid):
            raise HTTPException(400, "invalid chat id")
        return cid

    @app.get("/api/chats", dependencies=guard)
    def chats_list():
        """This user's saved conversations (newest first): id, title, dates, number of questions."""
        return {"chats": chat_store().list()}

    @app.get("/api/chats/{cid}", dependencies=guard)
    def chats_get(cid: str):
        chat = chat_store().get(chat_id(cid))
        if chat is None:
            raise HTTPException(404, "conversation not found")
        return chat

    @app.put("/api/chats/{cid}", dependencies=guard)
    def chats_put(cid: str, chat: dict):
        try:
            return chat_store().save(chat_id(cid), chat)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(500, f"the conversation could not be saved: {exc.__class__.__name__}")

    @app.delete("/api/chats/{cid}", dependencies=guard)
    def chats_delete(cid: str):
        if not chat_store().delete(chat_id(cid)):
            raise HTTPException(404, "conversation not found")
        return {"ok": True}

    @app.delete("/api/chats", dependencies=guard)
    def chats_clear():
        return {"ok": True, "removed": chat_store().clear()}

    # ------------------------------------------------------------------ usage log
    @app.get("/api/usage", dependencies=guard)
    def usage_get():
        """Where the usage log is written and, per user, how many questions and tokens it holds."""
        c: Config = state["cfg"]
        log_ = usage_log() or UsageLog(c.usage_log_dir, c.user_data_dir / "usage_local")
        return {"enabled": c.usage.enabled, "dir": str(c.usage_log_dir), "custom": bool(c.usage.log_dir),
                "user": current_user(), "machine": current_machine(), "file": str(log_.path),
                "last_error": log_.last_error, **log_.summary()}

    @app.post("/api/usage/open", dependencies=guard)
    def usage_open():
        """Desktop only: open the log folder in Explorer."""
        if not desktop:
            raise HTTPException(400, "only available in the desktop app")
        folder = state["cfg"].usage_log_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"folder not available: {exc.__class__.__name__}")
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
        return {"ok": True}

    # -------------------------------------------------------------- personal library
    def personal_info() -> dict:
        c: Config = state["cfg"]
        e = state["engine"]
        own = e.personal if e is not None else None
        buckets = [b for b in e.store.buckets() if b.get("personal")] if (e is not None and own is not None) else []
        return {"dir": str(c.personal_dir), "exists": c.personal_dir.is_dir(), "indexed": own is not None,
                "documents": sum(b["documents"] for b in buckets), "buckets": buckets,
                "folders": linked_folders(own), "can_add": c.server.allow_upload}

    def linked_folders(own) -> list[dict]:
        """Folders picked as personal buckets (read in place) and the bucket their documents are in."""
        from techrag.library import PERSONAL_BUCKET_PREFIX

        if own is None:
            return []
        out = []
        for r in own.roots():
            if r.get("kind") != "bucket":
                continue
            docs = own.root_documents(r["id"])
            out.append({"id": r["id"], "path": r["path"], "name": r["label"] or Path(r["path"]).name,
                        "documents": len(docs), "status": r.get("last_status", ""),
                        "bucket_id": PERSONAL_BUCKET_PREFIX + docs[0].bucket_id if docs else ""})
        return out

    def check_can_add_personal() -> Path:
        c: Config = state["cfg"]
        if not c.server.allow_upload:
            raise HTTPException(403, "adding documents is switched off on this server (server.allow_upload)")
        eng()
        try:
            c.personal_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"the personal folder cannot be created: {exc.__class__.__name__}")
        return c.personal_dir

    @app.get("/api/personal", dependencies=guard)
    def personal_get():
        return personal_info()

    @app.post("/api/personal/add", dependencies=guard)
    def personal_add(req: PathsRequest):
        """Copy the picked files into the personal folder and index them into the personal library."""
        folder = check_can_add_personal()
        copied = []
        for p in req.paths:
            src = Path(p)
            if not src.is_file() or src.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            dest = unique_dest(folder, _safe_filename(src.name), src)
            if not dest.exists():
                shutil.copy2(src, dest)
            copied.append(str(dest))
        if not copied:
            raise HTTPException(400, f"no supported files ({', '.join(sorted(SUPPORTED_SUFFIXES))})")
        return {"job": start_job("personal", personal=True)["id"], "copied": copied}

    @app.post("/api/personal/upload", dependencies=guard)
    async def personal_upload(file: UploadFile = File(...)):
        folder = check_can_add_personal()
        name = _safe_filename(file.filename or "upload")
        if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            raise HTTPException(400, f"unsupported file type; allowed: {', '.join(sorted(SUPPORTED_SUFFIXES))}")
        dest = unique_dest(folder, name)
        with open(dest, "wb") as fh:
            while chunk := await file.read(1 << 20):
                fh.write(chunk)
        return {"job": start_job("personal", personal=True)["id"], "path": str(dest)}

    @app.delete("/api/personal/documents/{doc_id}", dependencies=guard)
    def personal_delete(doc_id: int):
        """Remove a personal document: out of the personal index, and its file in the personal folder to the Recycle
        Bin (or to <personal folder>/_indeks/silinenler where there is none). The file the user originally picked is
        a different file and is not touched; a file outside the personal folder is never touched."""
        from techrag.library import PERSONAL_OFFSET, is_personal
        from techrag.trash import move_to_trash

        e = eng()
        own = e.personal.document(doc_id - PERSONAL_OFFSET) if is_personal(doc_id) and e.personal else None
        if own is None:
            raise HTTPException(404, "not a personal document")
        if jobs.running():
            raise HTTPException(409, "indexing is running; remove the document when it has finished")
        c: Config = state["cfg"]
        path = e.personal.source_path(own, c.personal_config().sources_dir)
        folder = c.personal_dir.resolve()
        trash, where = "missing", ""
        try:
            resolved = path.resolve()
            inside = folder in resolved.parents and (folder / PERSONAL_INDEX_DIR) not in resolved.parents
        except OSError:
            inside = False
        if path.is_file():
            if inside:
                try:
                    trash, moved = move_to_trash(path, c.personal_config().library_dir / "silinenler")
                    where = str(moved)
                except OSError as exc:
                    raise HTTPException(500, f"the file could not be moved: {exc.__class__.__name__}")
            else:
                trash, where = "kept", str(path)
        if trash == "kept":   # e.g. a document of a linked folder: the file stays, rescans leave it out
            e.personal.exclude_document(own.id)
        else:
            e.personal.delete_document(own.id)
        return {"ok": True, "trash": trash, "path": where, "title": own.title}

    @app.post("/api/personal/folder", dependencies=guard)
    def personal_folder(req: FolderRequest):
        """A folder of the user's picked as one personal bucket: every document under it is indexed where it is
        (nothing copied, nothing written there) into the personal library."""
        check_can_add_personal()
        c: Config = state["cfg"]
        path = Path(req.path).expanduser()
        if not path.is_dir():
            raise HTTPException(400, "folder not found")
        try:
            folder, picked = c.personal_dir.resolve(), path.resolve()
        except OSError:
            raise HTTPException(400, "folder not reachable")
        if picked == folder or folder in picked.parents:
            raise HTTPException(400, "this folder is already in the personal folder")
        if picked in folder.parents:
            raise HTTPException(400, "pick a folder that does not contain the personal folder")
        store, _ = eng().ensure_personal()
        known = next((r for r in store.roots() if same_path(r["path"], path)), None)
        root_id = known["id"] if known else store.add_root(str(path), path.name or str(path), kind="bucket")
        return {"root_id": root_id, "job": start_job("personal-folder", root_id=root_id, personal=True)["id"]}

    @app.delete("/api/personal/folders/{root_id}", dependencies=guard)
    def personal_folder_remove(root_id: int):
        """Unlink a folder picked as a personal bucket: its documents leave the personal index; the folder and its
        files are not touched."""
        e = eng()
        r = e.personal.root(root_id) if e.personal is not None else None
        if r is None or r.get("kind") != "bucket":
            raise HTTPException(404, "not a linked personal folder")
        if jobs.running():
            raise HTTPException(409, "indexing is running; remove the folder when it has finished")
        return {"ok": True, "removed": e.personal.remove_root(root_id), "name": r["label"]}

    PSTATUS_ORDER = {"failed": 0, "pending": 1, "partial": 2, "missing": 3, "indexed": 4}

    @app.get("/api/personal/status", dependencies=guard)
    def personal_status():
        """Every personal document and what the personal index holds of it: indexed (when; pages, pieces, tables),
        partly indexed (some table pages could not be read), waiting (a new file, or one changed since it was
        indexed - "Yeniden indeksle" processes it), failed, or its file gone. Only reads: the folders are listed (size
        and modification time; nothing is opened or hashed) and compared with the index."""
        from techrag.ingest.pipeline import ingest_fingerprint
        from techrag.ingest.sources import plan_changes, scan_root
        from techrag.library import PERSONAL_OFFSET

        c: Config = state["cfg"]
        e = eng()
        own = e.personal
        running = any(j["status"] in ("queued", "running") and str(j["kind"]).startswith("personal")
                      for j in list(jobs.items.values()))
        if own is None:
            return {"available": False, "running": running, "documents": [], "counts": {}, "unreachable": []}
        lib_cfg = c.personal_config()
        fp = ingest_fingerprint(lib_cfg, e.vision.model if e.vision else None)
        records = own.quality() or {}
        counts = own.doc_counts()
        plan: dict = {}
        unreachable: list[str] = []
        for r in own.roots():
            bucket = (r.get("label") or Path(r["path"]).name) if r.get("kind") == "bucket" else ""
            scan = scan_root(r["path"], exclude=(lib_cfg.library_dir,), single_bucket=bucket,
                             skip_rels=own.excluded_rels(r["id"]))
            if not scan.reachable:
                unreachable.append(r["label"] or Path(r["path"]).name)
            for it in plan_changes(scan, own.root_documents(r["id"]), fp, r["id"]):
                plan[f"@{r['id']}/{it.rel}"] = it

        def quality_of(rec: Optional[dict]) -> Optional[dict]:
            if not rec:
                return None
            data = rec.get("data") or {}
            return {"status": rec.get("status"), "processed_at": rec.get("processed_at", ""),
                    "index_kept": bool(rec.get("index_kept")), "last_ok_at": rec.get("last_ok_at", ""),
                    "issues": [i for i in (data.get("issues") or []) if i.get("code") != "vlm_disabled"][:40]}

        out = []
        for d in own.documents():
            rec, it = records.get(d.path), plan.get(d.path)
            action = it.action if it else ""
            if d.missing and action == "restored":
                status, reason = "pending", "restored"
            elif d.missing or action == "missing":
                status, reason = "missing", "missing"
            elif rec and rec.get("status") == "failed":
                status, reason = "failed", "failed_kept"
            elif action in ("changed", "settings"):
                status, reason = "pending", action
            elif rec and rec.get("status") == "partial":
                status, reason = "partial", "partial"
            else:
                status, reason = "indexed", ""
            n = counts.get(d.id, {})
            out.append({"doc_id": d.id + PERSONAL_OFFSET, "title": d.title, "file": d.rel_path or d.path,
                        "bucket": d.bucket, "pdf": (d.rel_path or d.path).lower().endswith(".pdf"), "status": status,
                        "reason": reason, "unreachable": action == "unreachable", "ingested_at": d.ingested_at,
                        "n_pages": d.n_pages, "n_chunks": d.n_chunks, "tables": n.get("tables", 0),
                        "tables_grounded": n.get("tables_grounded", 0), "parameters": n.get("parameters", 0),
                        "parameters_verified": n.get("parameters_verified", 0), "entities": list(d.entities),
                        "doc_type": d.doc_type, "revision": d.revision, "quality": quality_of(rec)})
        known = {d.path for d in own.documents()}
        for key, it in plan.items():
            if key in known or it.action != "new":
                continue
            rec = records.get(key)
            failed = bool(rec and rec.get("status") == "failed")
            out.append({"doc_id": None, "title": Path(it.rel).stem, "file": it.rel, "bucket": it.bucket,
                        "pdf": it.rel.lower().endswith(".pdf"), "status": "failed" if failed else "pending",
                        "reason": "failed_new" if failed else "new", "unreachable": False, "ingested_at": "",
                        "n_pages": 0, "n_chunks": 0, "tables": 0, "tables_grounded": 0, "parameters": 0,
                        "parameters_verified": 0, "entities": [], "doc_type": "", "revision": "",
                        "quality": quality_of(rec)})
        out.sort(key=lambda x: (PSTATUS_ORDER.get(x["status"], 9), x["bucket"].lower(), x["title"].lower()))
        tally: dict[str, int] = {}
        for x in out:
            tally[x["status"]] = tally.get(x["status"], 0) + 1
        return {"available": True, "running": running, "documents": out, "counts": tally, "unreachable": unreachable}

    @app.get("/api/personal/documents/{doc_id}/chunks", dependencies=guard)
    def personal_chunks(doc_id: int, offset: int = 0, limit: int = 40):
        """The pieces a personal document was split into, in reading order: section, pages, kind and the start of
        the text (search-only pieces - a table the vision model read that did not match the PDF text - marked)."""
        from techrag.ingest.chunker import estimate_tokens
        from techrag.library import PERSONAL_OFFSET, is_personal

        e = eng()
        own = e.personal.document(doc_id - PERSONAL_OFFSET) if is_personal(doc_id) and e.personal else None
        if own is None:
            raise HTTPException(404, "not a personal document")
        offset, limit = max(0, int(offset)), min(max(1, int(limit)), 200)
        rows = e.personal.neighbors(own.id, offset, offset + limit - 1)
        return {"total": own.n_chunks, "offset": offset,
                "chunks": [{"n": r.ordinal + 1, "section": r.section, "page_start": r.page_start,
                            "page_end": r.page_end, "kind": r.kind, "evidence": r.evidence,
                            "tokens": estimate_tokens(r.text), "text": r.text[:600],
                            "cut": len(r.text) > 600} for r in rows]}

    @app.post("/api/personal/rescan", dependencies=guard)
    def personal_rescan():
        """Index what changed in the personal folder (files copied or removed there by hand too)."""
        check_can_add_personal()
        return {"job": start_job("personal", personal=True)["id"]}

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
                    rep = migrate_index(c, e.shared, lambda m: job["messages"].append(m), gate=jobs.gate)
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
        e = eng().for_mode(req.mode)
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
                rep.update(set=label, retrieval_only=req.retrieval_only, model=e.model_info())
                path = save_report(rep, eval_dir())
                job["report"] = {"name": path.name, "summary": rep["summary"], "set": label, "model": rep["model"]}
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
