"""A fake OpenAI-compatible server (vLLM-like) for tests: /v1/models, /v1/embeddings, /v1/rerank,
/v1/chat/completions (SSE streaming, tool calls, reasoning_content). Behaviour is scripted per role by
looking at the system prompt (planner, metadata, VLM table extraction, judge, answer agent)."""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

import httpx

from techrag.embeddings import HashEmbedder

_EMB = HashEmbedder(dim=256)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def sources_in(messages) -> dict[int, str]:
    """n -> text of numbered sources visible in user/tool messages."""
    out: dict[int, str] = {}
    for m in messages:
        if m.get("role") not in ("user", "tool"):
            continue
        txt = _text(m.get("content"))
        for block in re.split(r"\n(?=\[\d+\] )|\n---\n", "\n" + txt):
            mm = re.match(r"\s*\[(\d+)\] (.*)", block, re.S)
            if mm:
                out[int(mm.group(1))] = mm.group(2)
    return out


class FakeOpenAI:
    def __init__(self):
        self.calls: list[dict] = []
        self.chat_models = ["qwen-test"]
        self.tools_supported = True
        self.vlm_hallucinate = False
        self.answer: Optional[Callable[[list, Optional[list]], dict]] = None  # override answer behaviour
        # override the judge: return the response text, an httpx.Response, or raise (e.g. httpx.ReadTimeout)
        self.judge: Optional[Callable[[list], object]] = None
        self.vlm: Optional[Callable[[str], dict]] = None  # override VLM table extraction (page text -> JSON)
        self.reasoning = ""
        self.rate_limited = 0      # answer this many embedding requests with HTTP 429 first
        self.retry_after = "2"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -------------------------------------------------------------------- routing
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.chat_models + ["emb-test", "rr-test"]]})
        body = json.loads(request.content or b"{}")
        self.calls.append({"path": path, "body": body})
        if path.endswith("/embeddings") and self.rate_limited > 0:
            self.rate_limited -= 1
            return httpx.Response(429, headers={"Retry-After": self.retry_after}, json={"error": "rate limited"})
        if path.endswith("/embeddings"):
            vecs = _EMB.embed_documents(body["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": v.tolist()} for i, v in enumerate(vecs)]})
        if path.endswith("/rerank"):
            q = set(re.findall(r"\w+", body["query"].lower()))
            res = []
            for i, d in enumerate(body["documents"]):
                w = set(re.findall(r"\w+", d.lower()))
                res.append({"index": i, "relevance_score": len(q & w) / (len(q) or 1)})
            return httpx.Response(200, json={"results": res})
        if path.endswith("/chat/completions"):
            return self.chat(body)
        return httpx.Response(404, json={"error": "not found"})

    # ----------------------------------------------------------------------- chat
    def chat(self, body: dict) -> httpx.Response:
        msgs = body["messages"]
        system = _text(msgs[0]["content"]) if msgs and msgs[0]["role"] == "system" else ""
        tools = body.get("tools")
        if tools and not self.tools_supported:
            return httpx.Response(400, json={"error": {"message": '"auto" tool choice requires --enable-auto-tool-choice'}})
        if "retrieval plan" in system:
            out = {"content": self._plan(msgs)}
        elif system.startswith("You read the first pages"):
            out = {"content": self._meta(msgs)}
        elif "extract tables" in system:
            out = {"content": self._vlm(msgs)}
        elif "strict fact checker" in system:
            if self.judge is not None:
                res = self.judge(msgs)
                if isinstance(res, httpx.Response):
                    return res
                out = {"content": res}
            else:
                out = {"content": self._judge(msgs)}
        elif "ANSWER CONTRACT" in system:
            out = (self.answer or self._answer)(msgs, tools)
        else:
            out = {"content": "OK"}
        return self._sse(out)

    def _sse(self, out: dict) -> httpx.Response:
        events = []
        if self.reasoning:
            events.append({"choices": [{"delta": {"reasoning_content": self.reasoning}}]})
        content = out.get("content") or ""
        for i in range(0, len(content), 9):
            events.append({"choices": [{"delta": {"content": content[i:i + 9]}}]})
        for idx, tc in enumerate(out.get("tool_calls") or []):
            args = json.dumps(tc["arguments"])
            events.append({"choices": [{"delta": {"tool_calls": [{"index": idx, "id": f"call_{idx}", "type": "function",
                                                                   "function": {"name": tc["name"], "arguments": ""}}]}}]})
            for i in range(0, len(args), 11):
                events.append({"choices": [{"delta": {"tool_calls": [{"index": idx, "function": {"arguments": args[i:i + 11]}}]}}]})
        events.append({"choices": [{"delta": {}, "finish_reason": "tool_calls" if out.get("tool_calls") else "stop"}]})
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    # --------------------------------------------------------------------- roles
    def _plan(self, msgs) -> str:
        q = _text(msgs[-1]["content"]).split("Question:", 1)[-1].strip()
        params = [p for p in ("tRFC", "tREFI", "VDD") if p.lower() in q.lower()]
        qtype = "comparison" if re.search(r"\b(vs|versus|fark|compare)\b", q, re.I) else ("numeric" if params else "lookup")
        return json.dumps({"standalone_question": q, "english_question": q, "search_queries": [q],
                           "keywords": params or q.split()[:4], "parameters": params, "question_type": qtype,
                           "needs_reasoning": qtype == "comparison"})

    def _meta(self, msgs) -> str:
        txt = _text(msgs[-1]["content"])
        std = re.search(r"\b(DDR\d)\b", txt)
        rev = re.search(r"Revision\s+(\w+)", txt)
        return json.dumps({"title": txt.split("\n")[0][:80], "standard": f"JESD79 {std.group(1)} SDRAM" if std else "",
                           "version": "", "revision": rev.group(1) if rev else "", "date": "", "doc_type": "base"})

    def _vlm(self, msgs) -> str:
        txt = _text(msgs[-1]["content"])
        if self.vlm is not None:
            return json.dumps(self.vlm(txt))
        m = re.search(r"tRFC\W+(\d+)", txt)
        if not m:
            return json.dumps({"tables": []})
        trfc = m.group(1)
        trefi = "7.9" if self.vlm_hallucinate else "7.8"
        return json.dumps({"tables": [{
            "caption": "Table 3-1 Timing parameters", "columns": ["Parameter", "Min", "Max", "Unit"],
            "rows": [["tRFC", trfc, "-", "ns"], ["tREFI", "-", trefi, "us"]], "footnotes": [],
            "parameters": [
                {"parameter": "Refresh cycle time", "symbol": "tRFC", "min": trfc, "typ": "", "max": "", "unit": "ns",
                 "conditions": "8Gb", "notes": ""},
                {"parameter": "Average refresh interval", "symbol": "tREFI", "min": "", "typ": "", "max": trefi,
                 "unit": "us", "conditions": "", "notes": ""}]}]})

    def _judge(self, msgs) -> str:
        txt = _text(msgs[-1]["content"])
        verdicts = []
        for block in re.split(r"\n\n(?=CLAIM )", txt):
            m = re.match(r"CLAIM (\d+): (.*?)\nTYPE: (\w+)\nCITED SOURCES:\n(.*)", block, re.S)
            if not m:
                continue
            claim, src = m.group(2), m.group(4)
            nums = re.findall(r"\d+(?:\.\d+)?", re.sub(r"\[\d+\]", "", claim))
            ok = all(n in src for n in nums) and "(none)" not in src
            verdicts.append({"id": int(m.group(1)), "verdict": "supported" if ok else "unsupported",
                             "reason": "" if ok else "not in source"})
        return json.dumps({"verdicts": verdicts})

    def _answer(self, msgs, tools) -> dict:
        last = _text(msgs[-1]["content"])
        if "FAILED verification" in last:
            draft = _text(msgs[-2]["content"])
            return {"content": "\n".join(l for l in draft.split("\n") if "3.9 us" not in l)}
        src = sources_in(msgs)
        used_tool = any(m.get("role") == "tool" for m in msgs)
        if tools and not used_tool and "trfc" in last.lower():
            std = re.search(r"\b(DDR\d)\b", last)
            return {"tool_calls": [{"name": "get_parameter",
                                    "arguments": {"name": "tRFC", "standard": std.group(1) if std else ""}}]}
        n, val = None, None
        for k in sorted(src):
            m = re.search(r"tRFC[^\n]*?(\d{3}) ?(?:\| ?-? ?\|? ?)?ns|tRFC[^\n]*?min=(\d+)", src[k])
            if m:
                n, val = k, m.group(1) or m.group(2)
                break
        if n is None:
            return {"content": "This information was not found in the loaded documents."}
        return {"content": (f"tRFC for an 8Gb device is {val} ns [{n}].\n\n### Documented\n"
                            f"- The refresh cycle time tRFC for an 8Gb device is {val} ns [{n}].\n"
                            f"- A REFRESH command must be issued every 3.9 us [{n}].\n\n"
                            f"### Engineering inference (not stated in the sources)\n"
                            f"- Plan refresh scheduling around the tRFC window [{n}].")}
