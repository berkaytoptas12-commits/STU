"""RAG engine: plan -> retrieve -> generate (streamed) -> verify (-> optional self-correction)."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional, Sequence

from techrag.config import Config
from techrag.domains import DomainRegistry
from techrag.embeddings import Embedder, create_embedder
from techrag.llm import LLMClient
from techrag.prompts import NOT_FOUND, SELF_CORRECT_PROMPT, answer_system_prompt, answer_user_prompt
from techrag.query import QueryPlan, QueryPlanner
from techrag.reranker import Reranker, create_reranker
from techrag.retrieval import Passage, RetrievalResult, Retriever
from techrag.store import Store
from techrag.verify import Verification, verify_answer

_UNSET = object()


@dataclass
class Answer:
    question: str
    answer: str
    passages: list[Passage]
    plan: Optional[QueryPlan]
    verification: Optional[Verification]
    confidence: Optional[float] = None
    routed_domains: list[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "sources": [p.to_dict() for p in self.passages],
            "plan": self.plan.to_dict() if self.plan else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "confidence": self.confidence,
            "routed_domains": self.routed_domains,
            "timings": self.timings,
        }


class RAGEngine:
    def __init__(self, cfg: Config, *, store: Optional[Store] = None, embedder: Optional[Embedder] = None,
                 reranker=_UNSET, llm: Optional[LLMClient] = None, domains: Optional[DomainRegistry] = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.domains = domains or DomainRegistry.load(cfg.paths.domains_file)
        self.llm = llm or LLMClient(cfg.llm)
        self._embedder = embedder
        self._reranker = reranker
        self._load_lock = threading.Lock()
        self.planner = QueryPlanner(self.domains, self.llm, cfg.retrieval.query_rewrite, cfg.answer.history_turns)

    # ------------------------------------------------------------- models
    @property
    def embedder(self) -> Embedder:
        with self._load_lock:
            if self._embedder is None:
                self._embedder = create_embedder(self.cfg.embedding)
        return self._embedder

    @property
    def reranker(self) -> Optional[Reranker]:
        with self._load_lock:
            if self._reranker is _UNSET:
                self._reranker = create_reranker(self.cfg.reranker)
        return self._reranker

    def check_index_compatibility(self) -> None:
        stored = self.store.get_meta("embedding_model")
        if stored and stored != self.embedder.name:
            raise RuntimeError(
                f"The index was built with '{stored}' but the configured embedder is '{self.embedder.name}'. "
                f"Fix config.yaml or rebuild the index: techrag ingest --rebuild"
            )

    def warmup(self) -> None:
        self.check_index_compatibility()
        self.embedder.embed_queries(["warmup"])
        if self.reranker:
            self.reranker.score("warmup", ["warmup"])
        self.store.vectors()

    @property
    def retriever(self) -> Retriever:
        return Retriever(self.cfg, self.store, self.embedder, self.reranker)

    # ----------------------------------------------------------- retrieval
    def retrieve(self, question: str, history: Optional[Sequence[dict]] = None,
                 domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
                 top_k: Optional[int] = None) -> RetrievalResult:
        t = time.time()
        plan = self.planner.plan(question, history)
        plan_time = time.time() - t
        self.check_index_compatibility()
        result = self.retriever.search(plan, domains=domains, doc_ids=doc_ids, top_k=top_k)
        result.timings["plan"] = round(plan_time, 3)
        return result

    # -------------------------------------------------------------- answer
    def _messages(self, plan: QueryPlan, passages: list[Passage], history: Sequence[dict]) -> list[dict]:
        msgs = [{"role": "system", "content": answer_system_prompt(plan.language)}]
        for m in list(history)[-2 * self.cfg.answer.history_turns:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            content = re.sub(r"\[\d+(?:\s*[,–-]\s*\d+)*\]", "", str(m.get("content", ""))).strip()
            if role == "assistant" and len(content) > 1500:
                content = content[:1500] + " ..."
            msgs.append({"role": role, "content": content})
        hints = self.domains.glossary_hints(plan.domains, f"{plan.question} {plan.standalone}")
        msgs.append({"role": "user", "content": answer_user_prompt(plan.standalone, passages, hints)})
        return msgs

    def ask_stream(self, question: str, history: Optional[Sequence[dict]] = None,
                   domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
                   top_k: Optional[int] = None) -> Iterator[dict]:
        """Yields events: plan, sources, token*, [replace], done."""
        t0 = time.time()
        history = list(history or [])
        result = self.retrieve(question, history, domains, doc_ids, top_k)
        plan, passages = result.plan, result.passages
        yield {"type": "plan", "plan": plan.to_dict(), "routed_domains": result.routed_domains,
               "planner_error": self.planner.last_error if not plan.rewritten_by_llm else None}
        yield {"type": "sources", "sources": [p.to_dict() for p in passages], "confidence": result.confidence}

        timings = dict(result.timings)
        if not passages:
            answer = NOT_FOUND.get(plan.language, NOT_FOUND["en"])
            if self.store.stats()["chunks"] == 0:
                answer += (" (İndeks boş: önce 'techrag ingest' çalıştırın.)" if plan.language == "tr"
                           else " (The index is empty: run 'techrag ingest' first.)")
            yield {"type": "token", "text": answer}
            yield {"type": "done", "answer": answer, "verification": None, "timings": timings}
            return

        messages = self._messages(plan, passages, history)
        t = time.time()
        parts: list[str] = []
        for piece in self.llm.stream(messages):
            parts.append(piece)
            yield {"type": "token", "text": piece}
        answer = "".join(parts).strip()
        timings["generate"] = round(time.time() - t, 3)

        verification = verify_answer(answer, passages, question) if self.cfg.answer.verify else None
        if verification and verification.needs_fix and self.cfg.answer.self_correct:
            t = time.time()
            problems = "\n".join(f"- {p}" for p in verification.problems())
            fixed = self.llm.chat(messages + [
                {"role": "assistant", "content": answer},
                {"role": "user", "content": SELF_CORRECT_PROMPT.format(problems=problems)},
            ])
            fixed_v = verify_answer(fixed, passages, question)
            timings["self_correct"] = round(time.time() - t, 3)
            if fixed.strip() and (len(fixed_v.unsupported_numbers) + len(fixed_v.invalid_citations)
                                  < len(verification.unsupported_numbers) + len(verification.invalid_citations)):
                answer, verification = fixed.strip(), fixed_v
                yield {"type": "replace", "text": answer}

        timings["total"] = round(time.time() - t0, 3)
        yield {"type": "done", "answer": answer,
               "verification": verification.to_dict() if verification else None, "timings": timings}

    def ask(self, question: str, history: Optional[Sequence[dict]] = None,
            domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
            top_k: Optional[int] = None) -> Answer:
        plan = None
        passages: list[Passage] = []
        confidence = None
        routed: list[str] = []
        answer = ""
        verification = None
        timings: dict = {}
        sources: list[dict] = []
        for ev in self.ask_stream(question, history, domains, doc_ids, top_k):
            if ev["type"] == "plan":
                plan = QueryPlan(**ev["plan"])
                routed = ev["routed_domains"]
            elif ev["type"] == "sources":
                sources = ev["sources"]
                confidence = ev["confidence"]
            elif ev["type"] == "done":
                answer = ev["answer"]
                timings = ev["timings"]
                if ev["verification"]:
                    vd = {k: v for k, v in ev["verification"].items() if k not in ("status", "problems")}
                    verification = Verification(**vd)
        passages = [Passage(**s) for s in sources]
        return Answer(question, answer, passages, plan, verification, confidence, routed, timings)
