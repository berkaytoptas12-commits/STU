import json

import httpx
import pytest
from conftest import FakeLLM

from techrag.config import LLMConfig, load_config
from techrag.engine import RAGEngine
from techrag.ingest.pipeline import Ingestor
from techrag.llm import LLMClient, LLMError
from techrag.retrieval import Passage
from techrag.store import Store
from techrag.verify import extract_numbers, parse_citations, verify_answer


def _passage(n, text, title="PCIe Base 5.0", section="4.2 Encoding"):
    return Passage(n, 1, title, "pcie/base.pdf", "pcie", section, 10, 11, "text", text, 0.9, [1], [1])


# ------------------------------------------------------------------ verify

def test_parse_citations_ranges_and_lists():
    assert parse_citations("a [1] b [2, 3] c [4-6] d [7–8]") == [1, 2, 3, 4, 5, 6, 7, 8]


def test_extract_numbers_skips_list_markers_and_citations():
    nums = extract_numbers("1. Gen3 uses 128b/130b at 8.0 GT/s [12].\n2. DDR4 VDD is 1.2 V")
    assert nums == ["128", "130", "8.0", "1.2"]


def test_verify_grounded_answer_is_ok():
    ps = [_passage(1, "PCI Express 3.0 uses 128b/130b encoding at a data rate of 8.0 GT/s per lane.")]
    v = verify_answer("PCIe 3.0, 128b/130b kodlama kullanır ve şerit başına 8,0 GT/s hızındadır [1].", ps)
    assert v.status == "ok", v.problems()
    assert v.citations_used == [1]


def test_verify_flags_invented_numbers_and_bad_citations():
    ps = [_passage(1, "The data rate is 8.0 GT/s per lane.")]
    v = verify_answer("The data rate is 16.0 GT/s per lane [1], and latency is 120 ns [3].", ps)
    assert v.status == "warning"
    assert v.unsupported_numbers == ["16.0", "120"]
    assert v.invalid_citations == [3]
    assert v.needs_fix


def test_verify_no_partial_number_matches():
    ps = [_passage(1, "tRFC is 350 ns and VDD is 1.25 V.")]
    v = verify_answer("VDD is 1.2 V and tRFC is 35 ns [1].", ps)
    assert v.unsupported_numbers == ["1.2", "35"]


def test_verify_detects_not_found():
    v = verify_answer("Bu bilgi sağlanan kaynaklarda bulunamadı.", [_passage(1, "x")])
    assert v.not_found and v.status == "not_found"


# --------------------------------------------------------------------- llm

def test_ollama_streaming_and_think_filter():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["options"]["num_ctx"] == 16384
        assert body["think"] is False
        lines = [{"message": {"content": "<think>hmm</think>"}}, {"message": {"content": "Merhaba"}},
                 {"message": {"content": " dünya [1]"}}, {"done": True}]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))

    client = LLMClient(LLMConfig(), transport=httpx.MockTransport(handler))
    assert client.chat([{"role": "user", "content": "hi"}]) == "Merhaba dünya [1]"


def test_ollama_retries_without_think_flag():
    seen = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        seen.append("think" in body)
        if "think" in body:
            return httpx.Response(400, json={"error": "model does not support thinking"})
        return httpx.Response(200, text=json.dumps({"message": {"content": "ok"}, "done": True}))

    client = LLMClient(LLMConfig(), transport=httpx.MockTransport(handler))
    assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert seen == [True, False]


def test_openai_compatible_sse():
    def handler(request: httpx.Request):
        assert request.url.path == "/v1/chat/completions"
        events = [{"choices": [{"delta": {"content": "Fast-mode "}}]},
                  {"choices": [{"delta": {"content": "400 kbit/s [1]"}}]}]
        text = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text)

    cfg = LLMConfig(provider="openai", base_url="http://localhost:8001/v1", model="m")
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    assert client.chat([{"role": "user", "content": "q"}]) == "Fast-mode 400 kbit/s [1]"


def test_unreachable_server_raises_clear_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = LLMClient(LLMConfig(), transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="unreachable"):
        client.chat([{"role": "user", "content": "q"}])


# ------------------------------------------------------------------ engine

def _engine(cfg, registry, embedder, llm):
    store = Store(cfg.db_path)
    Ingestor(cfg, store, embedder, registry).run()
    return RAGEngine(cfg, store=store, embedder=embedder, reranker=None, llm=llm, domains=registry)


def test_engine_answer_with_citations_and_verification(cfg, sources, registry, embedder):
    llm = FakeLLM(answer="tRFC 8Gb bir cihaz için 350 ns'dir [1].")
    engine = _engine(cfg, registry, embedder, llm)
    ans = engine.ask("tRFC refresh cycle time değeri nedir?")
    assert ans.passages and "350" in ans.passages[0].text
    assert ans.verification.status == "ok"
    assert ans.plan.language == "tr"
    system, user = llm.calls[-1][0]["content"], llm.calls[-1][-1]["content"]
    assert "Answer language: Turkish" in system
    assert "[1] Document: ACME Bus Standard" in user and "Section:" in user


def test_engine_flags_hallucinated_value(cfg, sources, registry, embedder):
    engine = _engine(cfg, registry, embedder, FakeLLM(answer="tRFC is 260 ns [1]."))
    ans = engine.ask("What is tRFC?")
    assert ans.verification.status == "warning"
    assert "260" in ans.verification.unsupported_numbers


def test_engine_self_correction_replaces_bad_answer(cfg, sources, registry, embedder):
    class Correcting(FakeLLM):
        def stream(self, messages, json_mode=False, max_tokens=None, temperature=None):
            self.calls.append(list(messages))
            yield "tRFC is 350 ns [1]." if len(messages) > 3 and "could not be verified" in messages[-1]["content"] \
                else "tRFC is 260 ns [1]."

    cfg.answer.self_correct = True
    engine = _engine(cfg, registry, embedder, Correcting())
    events = list(engine.ask_stream("What is tRFC?"))
    assert any(e["type"] == "replace" for e in events)
    done = events[-1]
    assert done["answer"] == "tRFC is 350 ns [1]."
    assert done["verification"]["status"] == "ok"


def test_engine_empty_index(cfg, registry, embedder):
    engine = RAGEngine(cfg, embedder=embedder, reranker=None, llm=FakeLLM(), domains=registry)
    ans = engine.ask("I2C hızı nedir?")
    assert "bulunamadı" in ans.answer and "ingest" in ans.answer


# ------------------------------------------------------------------ config

def test_config_env_override(tmp_path, monkeypatch):
    p = tmp_path / "c.yaml"
    p.write_text("llm:\n  model: a\nretrieval:\n  final_top_k: 5\n", encoding="utf-8")
    monkeypatch.setenv("TECHRAG_LLM__MODEL", "qwen3:32b")
    monkeypatch.setenv("TECHRAG_RERANKER__ENABLED", "false")
    cfg = load_config(p)
    assert cfg.llm.model == "qwen3:32b"
    assert cfg.retrieval.final_top_k == 5
    assert cfg.reranker.enabled is False


def test_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("llm:\n  modle: typo\n", encoding="utf-8")
    with pytest.raises(ValueError, match="llm.modle"):
        load_config(p)


def test_default_config_file_loads():
    cfg = load_config("config/config.yaml")
    assert cfg.embedding.model == "models/bge-m3"
