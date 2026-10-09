"""Streaming chat client for OpenAI-compatible servers with tools, images and thinking control."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional, Sequence

import httpx

from techrag.api import APIClient, APIError
from techrag.config import LLMConfig, ServiceConfig

Message = dict


class LLMError(RuntimeError):
    pass


class ToolsUnsupported(LLMError):
    """The server rejected tool calling (vLLM without --enable-auto-tool-choice)."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""

    def args(self) -> dict:
        try:
            v = json.loads(self.arguments or "{}")
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}

    def to_message(self) -> dict:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments or "{}"}}


@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""


class ThinkFilter:
    """Splits inline <think>...</think> blocks (servers without a reasoning parser) out of the content."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.buf = ""

    def feed(self, text: str) -> tuple[str, str]:
        """Returns (content, reasoning) parts of the new text."""
        self.buf += text
        content, reasoning = [], []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                (reasoning if self.inside else content).append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            keep = max((k for k in range(1, len(tag)) if self.buf.endswith(tag[:k])), default=0)
            (reasoning if self.inside else content).append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(content), "".join(reasoning)

    def flush(self) -> tuple[str, str]:
        rest, self.buf = self.buf, ""
        return ("", rest) if self.inside else (rest, "")


def strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return re.sub(r"^.*?</think>", "", text, flags=re.DOTALL).strip()


class LLMClient:
    def __init__(self, cfg: LLMConfig, transport: Optional[httpx.BaseTransport] = None,
                 service: Optional[ServiceConfig] = None):
        self.cfg = cfg
        self.api = APIClient(service or cfg, transport)
        self.model = (service or cfg).model
        self._send_template_kwargs = cfg.thinking_control == "chat_template"
        self._send_effort = cfg.thinking_control == "reasoning_effort"
        self._json_mode_ok = True
        self.tools_supported: Optional[bool] = None if cfg.tools == "auto" else cfg.tools == "on"

    # ------------------------------------------------------------------ payload
    def _payload(self, messages, tools, thinking, json_mode, max_tokens, temperature) -> dict:
        p: dict = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        if tools:
            p["tools"] = tools
            p["tool_choice"] = "auto"
        if self._send_template_kwargs:
            p["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
        elif self._send_effort and thinking:
            p["reasoning_effort"] = self.cfg.reasoning_effort
        if json_mode and self._json_mode_ok:
            p["response_format"] = {"type": "json_object"}
        return p

    # ------------------------------------------------------------------- stream
    def stream(self, messages: Sequence[Message], *, tools: Optional[list] = None, thinking: bool = False,
               json_mode: bool = False, max_tokens: Optional[int] = None,
               temperature: Optional[float] = None) -> Iterator[tuple[str, object]]:
        """Yields ("content", str), ("reasoning", str) and finally ("result", ChatResult)."""
        if not self.model:
            raise LLMError("no chat model configured (Settings > Chat model)")
        for attempt in range(4):
            payload = self._payload(messages, tools, thinking, json_mode, max_tokens, temperature)
            try:
                yield from self._stream_once(payload)
                return
            except APIError as exc:
                body = (exc.body or str(exc)).lower()
                if exc.status in (400, 422) and "chat_template_kwargs" in body and self._send_template_kwargs:
                    self._send_template_kwargs = False
                    continue
                if exc.status in (400, 422) and "reasoning_effort" in body and self._send_effort:
                    self._send_effort = False
                    continue
                if exc.status in (400, 422) and "response_format" in body and json_mode and self._json_mode_ok:
                    self._json_mode_ok = False
                    continue
                if tools and exc.status in (400, 422) and "tool" in body:
                    self.tools_supported = False
                    raise ToolsUnsupported(str(exc)) from exc
                raise LLMError(str(exc)) from exc

    def _stream_once(self, payload: dict) -> Iterator[tuple[str, object]]:
        result = ChatResult()
        calls: dict[int, ToolCall] = {}
        filt = ThinkFilter()
        try:
            with self.api.http.stream("POST", self.api.url("chat/completions"), json=payload) as r:
                if r.status_code >= 400:
                    body = r.read().decode("utf-8", "replace")
                    raise APIError(f"LLM HTTP {r.status_code}: {body[:400]}", r.status_code, body)
                for line in r.iter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    obj = json.loads(data)
                    for choice in obj.get("choices") or []:
                        delta = choice.get("delta") or choice.get("message") or {}
                        rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if rc:
                            result.reasoning += rc
                            yield "reasoning", rc
                        text = delta.get("content") or ""
                        if text:
                            c, rs = filt.feed(text)
                            if rs:
                                result.reasoning += rs
                                yield "reasoning", rs
                            if c:
                                result.content += c
                                yield "content", c
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", len(calls))
                            call = calls.setdefault(idx, ToolCall(id=tc.get("id") or f"call_{idx}", name=""))
                            if tc.get("id"):
                                call.id = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                call.name += fn["name"]
                            if fn.get("arguments"):
                                args = fn["arguments"]
                                call.arguments += args if isinstance(args, str) else json.dumps(args)
                        if choice.get("finish_reason"):
                            result.finish_reason = choice["finish_reason"]
        except httpx.ConnectError as exc:
            raise LLMError(f"LLM server unreachable at {self.api.base} ({exc})") from exc
        c, rs = filt.flush()
        if c:
            result.content += c
            yield "content", c
        if rs:
            result.reasoning += rs
        result.tool_calls = [calls[k] for k in sorted(calls) if calls[k].name]
        if result.tool_calls and self.tools_supported is None:
            self.tools_supported = True
        yield "result", result

    def chat(self, messages: Sequence[Message], **kw) -> ChatResult:
        result = ChatResult()
        for kind, value in self.stream(messages, **kw):
            if kind == "result":
                result = value  # type: ignore[assignment]
        result.content = strip_think(result.content) if "</think>" in result.content else result.content.strip()
        return result

    def text(self, messages: Sequence[Message], **kw) -> str:
        return self.chat(messages, **kw).content

    def list_models(self) -> list[str]:
        return self.api.list_models()


def parse_json_object(text: str) -> Optional[dict]:
    """First JSON object in model output (tolerates code fences and surrounding prose)."""
    text = strip_think(text or "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
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


def image_part(png_bytes: bytes) -> dict:
    import base64

    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}}
