"""Shared helpers for OpenAI-compatible HTTP endpoints (vLLM, SGLang, llama.cpp, LM Studio ...)."""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlparse

import httpx

from techrag import tls
from techrag.config import ServiceConfig, TLSConfig

# Process-wide TLS trust settings (set from the loaded config; see techrag/tls.py).
_TLS = TLSConfig()


def configure_tls(cfg: TLSConfig) -> None:
    global _TLS
    _TLS = cfg
    tls.clear_cache()


def current_tls() -> TLSConfig:
    return _TLS


class APIError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: str = "", code: str = "",
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.code = code
        self.retry_after = retry_after


RETRY_STATUS = (429, 503)
# A connection that drops or is refused for a moment (the network, a server restarting) is tried again this many times
# before the request fails. Only for the requests that give the same result when repeated (embeddings, reranking); a
# certificate problem or a timeout is never repeated.
NETWORK_RETRIES = 2
_sleep = time.sleep  # patched in tests


def transient_network_error(exc: BaseException) -> bool:
    """A connection refused, reset or dropped mid-request - not a certificate problem (repeating cannot fix that) and
    not a timeout (repeating a request that ran for minutes would only double the wait)."""
    if isinstance(exc, httpx.TimeoutException):
        return False
    if isinstance(exc, httpx.ConnectError):
        return tls.classify(exc) == tls.CONNECT
    return isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError))


def error_code(exc: BaseException) -> str:
    """What kind of failure ended a question, for a message the user can act on (the UI words it; the technical text
    stays available as detail): memory | connect | tls | timeout | auth | "" (anything else)."""
    e: Optional[BaseException] = exc
    for _ in range(8):
        if e is None:
            break
        if isinstance(e, MemoryError):
            return "memory"
        if isinstance(e, httpx.TimeoutException):
            return "timeout"
        code = getattr(e, "code", "")
        if code == tls.CONNECT:
            return "connect"
        if isinstance(code, str) and code.startswith("tls_"):
            return "tls"
        if getattr(e, "status", 0) in (401, 403):
            return "auth"
        if isinstance(e, httpx.ConnectError):
            return "connect" if tls.classify(e) == tls.CONNECT else "tls"
        e = e.__cause__ or e.__context__
    return ""


def retry_after_seconds(headers) -> Optional[float]:
    """Retry-After as seconds (delta-seconds or an HTTP date); None when absent/unparseable."""
    value = (headers or {}).get("retry-after") if hasattr(headers, "get") else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone

        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def backoff(attempt: int, retry_after: Optional[float], max_wait: float) -> float:
    """The server's own wait if it gave one, else 1, 2, 4 ... seconds; never more than max_wait."""
    wait = retry_after if retry_after is not None else float(2 ** attempt)
    return min(max(wait, 0.0), max_wait)


def normalize_base_url(url: str) -> str:
    """'http://host:8000' -> 'http://host:8000/v1'; keeps explicit paths ('.../v1', '/openai/v1')."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if "://" not in url:
        url = "http://" + url
    if urlparse(url).path in ("", "/"):
        url += "/v1"
    return url


def connect_error(exc: BaseException, base: str) -> APIError:
    code, message = tls.explain(exc, base)
    return APIError(message, code=code)


def make_http_client(svc: ServiceConfig, transport: Optional[httpx.BaseTransport] = None,
                     tls_cfg: Optional[TLSConfig] = None) -> httpx.Client:
    t = tls_cfg or _TLS
    headers = {"Authorization": f"Bearer {svc.api_key}"} if svc.api_key else {}
    kwargs = {}
    if transport is None:
        kwargs["verify"] = tls.httpx_verify(getattr(svc, "verify_ssl", True), t.system_store, t.ca_bundle)
    return httpx.Client(timeout=httpx.Timeout(svc.timeout, connect=10.0), headers=headers, transport=transport,
                        trust_env=t.use_system_proxy, **kwargs)


class APIClient:
    def __init__(self, svc: ServiceConfig, transport: Optional[httpx.BaseTransport] = None,
                 tls_cfg: Optional[TLSConfig] = None):
        self.svc = svc
        self.http = make_http_client(svc, transport, tls_cfg)

    @property
    def base(self) -> str:
        return normalize_base_url(self.svc.base_url)

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def post(self, path: str, payload: dict) -> dict:
        if not self.base:
            raise APIError("endpoint base URL is not configured", code="config")
        retries = max(0, int(getattr(self.svc, "max_retries", 3)))
        dropped = 0
        attempt = 0
        while True:
            try:
                r = self.http.post(self.url(path), json=payload)
            except httpx.TransportError as exc:
                if transient_network_error(exc) and dropped < NETWORK_RETRIES:
                    _sleep(backoff(dropped, None, 4.0))   # 1 s, then 2 s
                    dropped += 1
                    continue
                if isinstance(exc, httpx.ConnectError):
                    raise connect_error(exc, self.base) from exc
                raise
            if r.status_code in RETRY_STATUS and attempt < retries:
                _sleep(backoff(attempt, retry_after_seconds(r.headers), getattr(self.svc, "max_retry_wait", 60.0)))
                attempt += 1
                continue
            if r.status_code >= 400:
                raise APIError(f"{self.base}/{path.lstrip('/')} -> HTTP {r.status_code}: {r.text[:400]}",
                               r.status_code, r.text, code="http", retry_after=retry_after_seconds(r.headers))
            return r.json()

    def model_entries(self) -> list[dict]:
        """The raw /models entries (vLLM adds max_model_len to each)."""
        if not self.base:
            raise APIError("endpoint base URL is not configured", code="config")
        try:
            r = self.http.get(self.url("models"), timeout=15)
        except httpx.ConnectError as exc:
            raise connect_error(exc, self.base) from exc
        if r.status_code >= 400:
            raise APIError(f"HTTP {r.status_code}: {r.text[:300]}", r.status_code, r.text, code="http")
        data = r.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        return [m for m in items or [] if isinstance(m, dict)]

    def list_models(self) -> list[str]:
        if not self.base:
            raise APIError("endpoint base URL is not configured", code="config")
        try:
            r = self.http.get(self.url("models"), timeout=15)
        except httpx.ConnectError as exc:
            raise connect_error(exc, self.base) from exc
        if r.status_code >= 400:
            raise APIError(f"HTTP {r.status_code}: {r.text[:300]}", r.status_code, r.text, code="http")
        data = r.json()
        items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
        names = []
        for m in items or []:
            name = m.get("id") or m.get("name") if isinstance(m, dict) else str(m)
            if name:
                names.append(name)
        return names


def check_service(svc: ServiceConfig, transport=None) -> dict:
    """Reachability + model presence, for the Settings dialog and `techrag doctor`."""
    try:
        models = APIClient(svc, transport).list_models()
    except Exception as exc:
        return {"ok": False, "models": [], "error": str(exc), "code": getattr(exc, "code", "") or "error"}
    ok = not svc.model or svc.model in models
    return {"ok": ok, "models": models, "error": None if ok else f"model '{svc.model}' is not served here",
            "code": "" if ok else "model"}
