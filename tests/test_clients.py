import json

import httpx
import pytest

from techrag.api import normalize_base_url
from techrag.config import EmbeddingConfig, LLMConfig, RerankerConfig
from techrag.embeddings import APIEmbedder, prefixes_for
from techrag.llm import LLMClient, LLMError, ThinkFilter, ToolsUnsupported, parse_json_object
from techrag.reranker import APIReranker


def sse(*events) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


def test_base_url_normalisation():
    assert normalize_base_url("gpu01:8000") == "http://gpu01:8000/v1"
    assert normalize_base_url("http://gpu01:8000/") == "http://gpu01:8000/v1"
    assert normalize_base_url("https://x/openai/v1") == "https://x/openai/v1"


def test_streaming_tool_calls_reasoning_and_think_tags():
    def handler(req):
        return httpx.Response(200, text=sse(
            {"choices": [{"delta": {"reasoning_content": "plan..."}}]},
            {"choices": [{"delta": {"content": "<thi"}}]},
            {"choices": [{"delta": {"content": "nk>inline</think>Hello"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "get_parameter", "arguments": "{\"na"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "me\": \"tRFC\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))

    client = LLMClient(LLMConfig(model="m"), transport=httpx.MockTransport(handler))
    res = client.chat([{"role": "user", "content": "q"}], tools=[{"type": "function"}])
    assert res.content == "Hello"
    assert res.reasoning == "plan...inline"
    assert res.tool_calls[0].name == "get_parameter" and res.tool_calls[0].args() == {"name": "tRFC"}
    assert res.finish_reason == "tool_calls"


def test_thinking_flag_fallback_and_tools_unsupported():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json={"error": "unknown field chat_template_kwargs"})
        if "tools" in body:
            return httpx.Response(400, json={"error": "\"auto\" tool choice requires --enable-auto-tool-choice"})
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "ok"}}]}))

    client = LLMClient(LLMConfig(model="m"), transport=httpx.MockTransport(handler))
    assert client.chat([{"role": "user", "content": "q"}], thinking=True).content == "ok"
    assert "chat_template_kwargs" in seen[0] and "chat_template_kwargs" not in seen[1]
    with pytest.raises(ToolsUnsupported):
        client.chat([{"role": "user", "content": "q"}], tools=[{"type": "function"}])
    assert client.tools_supported is False


def test_reasoning_effort_control():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "x"}}]}))

    client = LLMClient(LLMConfig(model="m", thinking_control="reasoning_effort", reasoning_effort="high"),
                       transport=httpx.MockTransport(handler))
    client.chat([{"role": "user", "content": "q"}], thinking=True)
    client.chat([{"role": "user", "content": "q"}], thinking=False)
    assert bodies[0]["reasoning_effort"] == "high" and "reasoning_effort" not in bodies[1]


def test_unreachable_server():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="unreachable|connect"):
        LLMClient(LLMConfig(model="m"), transport=httpx.MockTransport(handler)).chat([{"role": "user", "content": "q"}])


def test_embedding_prefixes_by_model():
    q, p = prefixes_for("Qwen/Qwen3-Embedding-0.6B")
    assert q.startswith("Instruct: ") and q.endswith("Query: ") and p == ""
    assert prefixes_for("intfloat/multilingual-e5-large") == ("query: ", "passage: ")
    assert prefixes_for("BAAI/bge-m3") == ("", "")
    assert prefixes_for("BAAI/bge-m3", query_instruction="Q: ") == ("Q: ", "")


def test_embedder_batches_keep_order():
    def handler(req):
        body = json.loads(req.content)
        data = [{"index": i, "embedding": [float(len(t)), 1.0]} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))})

    emb = APIEmbedder(EmbeddingConfig(model="bge-m3", base_url="http://x/v1", batch_size=2, concurrency=3),
                      transport=httpx.MockTransport(handler))
    out = emb.embed_documents(["a", "bbb", "cc", "dddd", "e"])
    ratios = out[:, 0] / out[:, 1]
    assert list(ratios.round(3)) == [1.0, 3.0, 2.0, 4.0, 1.0]


def test_qwen3_reranker_template_and_score_fallback():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.url.path, body))
        if req.url.path.endswith("/rerank"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"data": [{"index": 0, "score": 2.0}, {"index": 1, "score": -2.0}]})

    rr = APIReranker(RerankerConfig(model="Qwen/Qwen3-Reranker-0.6B", base_url="http://x/v1"),
                     transport=httpx.MockTransport(handler))
    scores = rr.score("LTSSM", ["doc a", "doc b"])
    assert scores[0] > 0.8 and scores[1] < 0.2, "logits are mapped to probabilities"
    assert seen[1][0].endswith("/score")
    assert seen[1][1]["text_1"].startswith("<|im_start|>system") and "<Query>: LTSSM" in seen[1][1]["text_1"]
    assert seen[1][1]["text_2"][0].startswith("<Document>: doc a")


def test_parse_json_object_tolerates_fences():
    assert parse_json_object('Sure:\n```json\n{"a": "x}", "b": [1]}\n```') == {"a": "x}", "b": [1]}
    f = ThinkFilter()
    assert f.feed("a<think>b</think>c") == ("ac", "b")
