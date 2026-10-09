"""Minimal chat client for local LLM servers (Ollama native API or any OpenAI-compatible server)."""

from __future__ import annotations

import json
import re
from typing import Iterator, Optional, Sequence

import httpx

from techrag.config import LLMConfig

Message = dict  # {"role": "system"|"user"|"assistant", "content": str}


class LLMError(RuntimeError):
    pass


class ThinkFilter:
    """Removes <think>...</think> reasoning blocks from a token stream (tags may span chunks)."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.buf = ""

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while self.buf:
            if self.inside:
                idx = self.buf.find(self.CLOSE)
                if idx < 0:
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]
                    break
                self.buf = self.buf[idx + len(self.CLOSE):]
                self.inside = False
            else:
                idx = self.buf.find(self.OPEN)
                if idx >= 0:
                    out.append(self.buf[:idx])
                    self.buf = self.buf[idx + len(self.OPEN):]
                    self.inside = True
                    continue
                # Hold back a possible partial "<think" at the end.
                keep = 0
                for k in range(1, len(self.OPEN)):
                    if self.buf.endswith(self.OPEN[:k]):
                        keep = k
                out.append(self.buf[:len(self.buf) - keep])
                self.buf = self.buf[len(self.buf) - keep:]
                break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return re.sub(r"^.*?</think>", "", text, flags=re.DOTALL).strip()


class LLMClient:
    def __init__(self, cfg: LLMConfig, transport: Optional[httpx.BaseTransport] = None):
        self.cfg = cfg
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self.client = httpx.Client(timeout=httpx.Timeout(cfg.timeout, connect=10.0), headers=headers,
                                   transport=transport)
        self._send_think = cfg.think is not None

    @property
    def base(self) -> str:
        return self.cfg.base_url.rstrip("/")

    # ---------------------------------------------------------------- public
    def chat(self, messages: Sequence[Message], *, json_mode: bool = False, max_tokens: Optional[int] = None,
             temperature: Optional[float] = None) -> str:
        return strip_think("".join(self.stream(messages, json_mode=json_mode, max_tokens=max_tokens,
                                               temperature=temperature)))

    def stream(self, messages: Sequence[Message], *, json_mode: bool = False, max_tokens: Optional[int] = None,
               temperature: Optional[float] = None) -> Iterator[str]:
        filt = ThinkFilter()
        source = self._ollama if self.cfg.provider == "ollama" else self._openai
        try:
            for piece in source(list(messages), json_mode, max_tokens, temperature):
                text = filt.feed(piece)
                if text:
                    yield text
        except httpx.ConnectError as exc:
            raise LLMError(f"LLM server unreachable at {self.base} ({exc}). Is it running?") from exc
        rest = filt.flush()
        if rest:
            yield rest

    def health(self) -> dict:
        """Check that the server is up and the configured model is available."""
        try:
            if self.cfg.provider == "ollama":
                r = self.client.get(f"{self.base}/api/tags", timeout=10)
                r.raise_for_status()
                names = [m.get("name", "") for m in r.json().get("models", [])]
            else:
                r = self.client.get(f"{self.base}/models", timeout=10)
                r.raise_for_status()
                names = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception as exc:
            return {"ok": False, "error": str(exc), "models": []}
        wanted = self.cfg.model
        present = any(n == wanted or n.split(":")[0] == wanted or n == f"{wanted}:latest" for n in names)
        return {"ok": present, "models": names,
                "error": None if present else f"model '{wanted}' not found on server"}

    # ------------------------------------------------------------- backends
    def _ollama(self, messages, json_mode, max_tokens, temperature) -> Iterator[str]:
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "keep_alive": "30m",
            "options": {
                "temperature": self.cfg.temperature if temperature is None else temperature,
                "num_ctx": self.cfg.num_ctx,
                "num_predict": max_tokens or self.cfg.max_tokens,
            },
        }
        if json_mode:
            payload["format"] = "json"
        if self._send_think:
            payload["think"] = self.cfg.think
        with self.client.stream("POST", f"{self.base}/api/chat", json=payload) as r:
            if r.status_code >= 400:
                body = r.read().decode("utf-8", "replace")
                if self._send_think and "think" in body.lower():
                    # Older Ollama or a model without a thinking switch: retry without the flag.
                    self._send_think = False
                    yield from self._ollama(messages, json_mode, max_tokens, temperature)
                    return
                raise LLMError(f"Ollama error {r.status_code}: {body[:500]}")
            for line in r.iter_lines():
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise LLMError(f"Ollama error: {data['error']}")
                content = (data.get("message") or {}).get("content") or ""
                if content:
                    yield content
                if data.get("done"):
                    break

    def _openai(self, messages, json_mode, max_tokens, temperature) -> Iterator[str]:
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        with self.client.stream("POST", f"{self.base}/chat/completions", json=payload) as r:
            if r.status_code >= 400:
                raise LLMError(f"LLM server error {r.status_code}: {r.read().decode('utf-8', 'replace')[:500]}")
            for line in r.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or choices[0].get("message") or {}
                content = delta.get("content") or ""
                if content:
                    yield content


def parse_json_object(text: str) -> Optional[dict]:
    """Extract the first JSON object from model output (tolerates code fences and chatter)."""
    text = strip_think(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
