"""Shared helpers for OpenAI-compatible HTTP endpoints (vLLM, SGLang, llama.cpp, LM Studio ...)."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

import httpx

from techrag.config import ServiceConfig


class APIError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


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


class APIClient:
    def __init__(self, svc: ServiceConfig, transport: Optional[httpx.BaseTransport] = None):
        self.svc = svc
        headers = {"Authorization": f"Bearer {svc.api_key}"} if svc.api_key else {}
        self.http = httpx.Client(timeout=httpx.Timeout(svc.timeout, connect=10.0), headers=headers,
                                 transport=transport)

    @property
    def base(self) -> str:
        return normalize_base_url(self.svc.base_url)

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def post(self, path: str, payload: dict) -> dict:
        if not self.base:
            raise APIError("endpoint base URL is not configured")
        try:
            r = self.http.post(self.url(path), json=payload)
        except httpx.ConnectError as exc:
            raise APIError(f"cannot connect to {self.base} ({exc})") from exc
        if r.status_code >= 400:
            raise APIError(f"{self.base}/{path.lstrip('/')} -> HTTP {r.status_code}: {r.text[:400]}",
                           r.status_code, r.text)
        return r.json()

    def list_models(self) -> list[str]:
        if not self.base:
            raise APIError("endpoint base URL is not configured")
        try:
            r = self.http.get(self.url("models"), timeout=15)
        except httpx.ConnectError as exc:
            raise APIError(f"cannot connect to {self.base} ({exc})") from exc
        if r.status_code >= 400:
            raise APIError(f"HTTP {r.status_code}: {r.text[:300]}", r.status_code, r.text)
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
        return {"ok": False, "models": [], "error": str(exc)}
    ok = not svc.model or svc.model in models
    return {"ok": ok, "models": models, "error": None if ok else f"model '{svc.model}' is not served here"}
