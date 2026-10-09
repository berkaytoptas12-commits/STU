"""The answering engine.

plan (LLM) -> scoped retrieval + typed parameter rows + figure pages -> tool-using agent loop (streamed)
-> deterministic verification -> entailment judge -> one regeneration -> strip/flag what still fails.

Events from ask_stream():
  plan, sources, sources_add, status, reasoning, tool, draft, draft_reset, final, error
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Optional, Sequence

from techrag.config import Config
from techrag.domains import DomainRegistry
from techrag.embeddings import Embedder, create_embedder
from techrag.ingest.vlm import render_page as _render
from techrag.llm import LLMClient, ToolsUnsupported, image_part, parse_json_object
from techrag.prompts import (JUDGE_SYSTEM, NOT_FOUND, REGENERATE_PROMPT, answer_system_prompt,
                             answer_user_prompt, judge_user_prompt)
from techrag.query import QueryPlan, QueryPlanner
from techrag.reranker import Reranker, create_reranker
from techrag.retrieval import DocCatalog, RetrievalResult, Retriever
from techrag.store import Store
from techrag.tools import TOOL_SCHEMAS, SourceRegistry, ToolExecutor
from techrag.verify import Claim, Verification, apply_failures, deterministic_check, is_not_found, split_claims

_UNSET = object()
PAGE_DESCRIBE_PROMPT = ("Transcribe this page of a technical standard for an engineer. For figures, timing diagrams, "
                        "waveforms, pinouts and state diagrams: give the figure title, every label, signal/pin name, "
                        "timing parameter and value exactly as printed, and how the elements relate (which edge "
                        "a parameter is measured between, state transitions, pin numbering). Do not infer anything "
                        "that is not visible.")
MAX_FIGURES = 2
JUDGE_SOURCE_CHARS = 6000


class RAGEngine:
    def __init__(self, cfg: Config, *, store: Optional[Store] = None, embedder: Optional[Embedder] = None,
                 reranker=_UNSET, llm: Optional[LLMClient] = None, vision: Optional[LLMClient] = _UNSET,
                 domains: Optional[DomainRegistry] = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path, read_only=cfg.read_only)
        self.domains = domains or DomainRegistry.load(cfg.domains_file)
        self.llm = llm or LLMClient(cfg.llm)
        self._vision = vision
        self._embedder = embedder
        self._reranker = reranker
        self._lock = threading.Lock()
        self.catalog = DocCatalog(self.store)
        self.planner = QueryPlanner(self.domains, self.llm, cfg.retrieval.query_rewrite, cfg.answer.history_turns)

    # ------------------------------------------------------------------ services
    @property
    def embedder(self) -> Embedder:
        with self._lock:
            if self._embedder is None:
                self._embedder = create_embedder(self.cfg.embedding)
        return self._embedder

    @property
    def reranker(self) -> Optional[Reranker]:
        with self._lock:
            if self._reranker is _UNSET:
                self._reranker = create_reranker(self.cfg.reranker)
        return self._reranker

    @property
    def vision(self) -> Optional[LLMClient]:
        with self._lock:
            if self._vision is _UNSET:
                svc = self.cfg.vision_service()
                self._vision = LLMClient(self.cfg.llm, service=svc) if (self.cfg.vision.enabled and svc.model) else None
        return self._vision

    @property
    def retriever(self) -> Retriever:
        return Retriever(self.cfg, self.store, self.embedder, self.reranker, self.catalog)

    def check_index_compatibility(self) -> None:
        stored = self.store.get_meta("embedding_model")
        if stored and stored != self.embedder.name:
            raise RuntimeError(f"The library was indexed with '{stored}' but the embedding setting is "
                               f"'{self.embedder.name}'. Change the setting back or re-index (ingest --rebuild).")

    # ------------------------------------------------------------------- pages
    def document_path(self, doc_id: int) -> Optional[Path]:
        doc = self.store.document(doc_id)
        if not doc:
            return None
        p = Path(doc.path)
        return p if p.is_absolute() else self.cfg.sources_dir / p

    def render_page(self, doc_id: int, page: int, dpi: Optional[int] = None) -> Optional[bytes]:
        doc = self.store.document(doc_id)
        path = self.document_path(doc_id)
        if not doc or not path or not path.exists() or path.suffix.lower() != ".pdf":
            return None
        dpi = dpi or self.cfg.vision.dpi
        cache = self.cfg.page_cache_dir / f"{doc.sha256[:20]}_p{page}_{dpi}.png"
        if cache.exists():
            return cache.read_bytes()
        png = _render(path, page, dpi)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(png)
        except OSError:
            pass
        return png

    @property
    def chat_sees_images(self) -> bool:
        """True when the chat model itself is the vision model (multimodal Qwen): images go to it directly.
        With a separate VLM, page images are transcribed by the VLM and the chat model gets text."""
        v = self.cfg.vision
        return v.enabled and (not v.base_url or v.base_url == self.cfg.llm.base_url) and \
            (not v.model or v.model == self.cfg.llm.model)

    def describe_page(self, doc_id: int, page: int) -> Optional[str]:
        """VLM transcription of a page (figures, timing diagrams, pinouts), cached next to the page renders."""
        doc = self.store.document(doc_id)
        if not doc or self.vision is None:
            return None
        cache = self.cfg.page_cache_dir / f"{doc.sha256[:20]}_p{page}_desc.txt"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        png = self.render_page(doc_id, page)
        if not png:
            return None
        try:
            text = self.vision.chat([{"role": "user", "content": [
                {"type": "text", "text": PAGE_DESCRIBE_PROMPT}, image_part(png)]}],
                max_tokens=1500, temperature=0.0, thinking=False).content.strip()
        except Exception:
            return None
        if text:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(text, encoding="utf-8")
            except OSError:
                pass
        return "(VLM transcription of the page image)\n" + text if text else None

    # --------------------------------------------------------------- retrieval
    def retrieve(self, question: str, history: Optional[Sequence[dict]] = None,
                 domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
                 top_k: Optional[int] = None) -> RetrievalResult:
        t = time.time()
        plan = self.planner.plan(question, history)
        plan_time = time.time() - t
        self.check_index_compatibility()
        res = self.retriever.search(plan, domains=domains, doc_ids=doc_ids, top_k=top_k)
        res.timings["plan"] = round(plan_time, 3)
        return res

    # ------------------------------------------------------------------ answer
    def _thinking(self, plan: QueryPlan) -> bool:
        mode = self.cfg.llm.thinking
        return mode == "on" or (mode == "auto" and plan.needs_reasoning)

    def _scope_note(self, res: RetrievalResult) -> str:
        s = res.scope
        if s.reason == "entity":
            return (f"Search scope: restricted to documents of {', '.join(s.entities)} (named in the question). "
                    f"Use the standard argument of the tools to look at other standards if needed.\n\n")
        if s.reason == "user":
            return "Search scope: restricted to the documents the user selected.\n\n"
        return ""

    def _history_messages(self, history: Sequence[dict]) -> list[dict]:
        out = []
        for m in list(history)[-2 * self.cfg.answer.history_turns:]:
            if m.get("role") not in ("user", "assistant"):
                continue
            content = re.sub(r"\[\d+(?:\s*[,–-]\s*\d+)*\]", "", str(m.get("content", ""))).strip()
            if m["role"] == "assistant" and len(content) > 1500:
                content = content[:1500] + " ..."
            out.append({"role": m["role"], "content": content})
        return out

    def ask_stream(self, question: str, history: Optional[Sequence[dict]] = None,
                   domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
                   top_k: Optional[int] = None) -> Iterator[dict]:
        t0 = time.time()
        history = list(history or [])
        yield {"type": "status", "stage": "planning"}
        res = self.retrieve(question, history, domains, doc_ids, top_k)
        plan = res.plan
        lang = plan.language
        thinking = self._thinking(plan)
        yield {"type": "plan", "plan": plan.to_dict(), "scope": {"reason": res.scope.reason,
               "entities": res.scope.entities, "domains": res.scope.domains}, "thinking": thinking,
               "planner_error": None if plan.rewritten_by_llm else self.planner.last_error}

        registry = SourceRegistry()
        for p in res.passages:
            registry.add_passage(p)
        docs = self.catalog.docs()
        for r in res.parameters:
            d = docs.get(r.doc_id)
            registry.add_parameter(r, {"entities": list(d.entities), "doc_type": d.doc_type,
                                       "revision": d.revision} if d else {})
        yield {"type": "sources", "sources": [s.to_dict() for s in registry.items], "confidence": res.confidence}

        timings = dict(res.timings)
        if self.store.stats()["chunks"] == 0:
            msg = NOT_FOUND[lang] + (" (Kütüphane boş: önce doküman ekleyip indeksleyin.)" if lang == "tr"
                                     else " (The library is empty: add and index documents first.)")
            yield {"type": "final", "answer": msg, "verification": Verification(not_found=True).summary(),
                   "sources": [], "timings": timings}
            return

        # Figures referenced by top passages go to the (multimodal) model as page images.
        images = []
        if self.cfg.vision.enabled:
            for p in res.passages:
                if not p.figure_page or len(images) >= MAX_FIGURES:
                    continue
                if self.chat_sees_images:
                    png = self.render_page(p.doc_id, p.figure_page)
                    if png:
                        src, _ = registry.add_page(p.doc_id, p.figure_page, p.doc_title,
                                                   "(page image attached)", {"entities": p.entities})
                        images.append((src.n, png))
                else:
                    desc = self.describe_page(p.doc_id, p.figure_page)
                    if desc:
                        registry.add_page(p.doc_id, p.figure_page, p.doc_title, desc, {"entities": p.entities})
                        images.append((0, None))
        images = [(n, png) for n, png in images if png]

        system = answer_system_prompt(lang)
        hints = self.domains.glossary_hints(plan.domains, f"{plan.question} {plan.standalone}")
        user_text = answer_user_prompt(plan.standalone, "\n\n---\n\n".join(s.prompt_text() for s in registry.items),
                                       hints, self._scope_note(res))
        if images:
            user_content: object = [{"type": "text", "text": user_text}] + [
                part for n, png in images for part in ({"type": "text", "text": f"Page image for source [{n}]:"},
                                                       image_part(png))]
        else:
            user_content = user_text
        messages = [{"role": "system", "content": system}, *self._history_messages(history),
                    {"role": "user", "content": user_content}]

        executor = ToolExecutor(self.store, self.retriever, registry, plan, self.render_page,
                                vision_input=self.chat_sees_images, describe_page=self.describe_page,
                                base_doc_ids=list(doc_ids) if doc_ids else None, expand=self.domains.expand_terms,
                                default_doc_ids=res.scope.doc_ids if res.scope.reason == "entity" else None)
        tools = TOOL_SCHEMAS if (self.cfg.llm.tools != "off" and self.llm.tools_supported is not False) else None
        max_rounds = self.cfg.llm.max_tool_rounds
        result = None
        t = time.time()
        yield {"type": "status", "stage": "answering"}
        round_no = 0
        while True:
            use_tools = tools if round_no < max_rounds else None
            try:
                for kind, value in self.llm.stream(messages, tools=use_tools, thinking=thinking):
                    if kind == "content":
                        yield {"type": "draft", "text": value}
                    elif kind == "reasoning":
                        yield {"type": "reasoning", "text": value}
                    elif kind == "result":
                        result = value
            except ToolsUnsupported:
                tools = None
                yield {"type": "status", "stage": "tools_unsupported"}
                continue
            if result is not None and result.tool_calls and use_tools:
                round_no += 1
                messages.append({"role": "assistant", "content": result.content or "",
                                 "tool_calls": [tc.to_message() for tc in result.tool_calls]})
                pngs = []
                for tc in result.tool_calls:
                    out = executor.run(tc.name, tc.args())
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out.text})
                    yield {"type": "tool", "name": tc.name, "args": tc.args(), "summary": out.summary}
                    if out.new_sources:
                        yield {"type": "sources_add", "sources": [s.to_dict() for s in out.new_sources]}
                    if out.image_png:
                        pngs.append(out.image_png)
                if pngs:
                    messages.append({"role": "user", "content": [{"type": "text", "text": "Requested page image(s):"}]
                                     + [image_part(p) for p in pngs]})
                yield {"type": "draft_reset"}
                continue
            break
        draft = (result.content if result else "").strip()
        timings["generate"] = round(time.time() - t, 3)

        # ------------------------------------------------------------ verification
        verification = Verification(not_found=is_not_found(draft))
        final = draft
        if self.cfg.answer.verify and draft:
            t = time.time()
            yield {"type": "status", "stage": "verifying"}
            verification = self._verify(draft, registry, question)
            if verification.failing and self.cfg.answer.regenerate:
                yield {"type": "status", "stage": "regenerating", "failed": len(verification.failing)}
                revised = self._regenerate(system, registry, plan, draft, verification, thinking)
                if revised:
                    ok_texts = {c.text for c in verification.claims if c.status == "ok"}
                    yield {"type": "status", "stage": "verifying"}
                    v2 = self._verify(revised, registry, question, known_ok=ok_texts)
                    v2.regenerated = True
                    v2.judge_used = v2.judge_used or verification.judge_used
                    if len(v2.failing) <= len(verification.failing):
                        draft, verification = revised, v2
            final, affected = apply_failures(draft, verification.claims, self.cfg.answer.failed_claims)
            if self.cfg.answer.failed_claims == "flag":
                verification.flagged = affected
            else:
                verification.removed = affected
                for c in verification.claims:
                    if c.status == "fail":
                        c.status = "removed"
                remaining = [c for c in verification.claims if c.status == "ok" and c.effective_section != "inference"]
                if affected and not remaining:
                    final = NOT_FOUND[lang] + "\n\n" + (
                        "(Üretilen ifadeler kaynaklarla doğrulanamadığı için kaldırıldı.)" if lang == "tr"
                        else "(The generated statements could not be verified against the sources and were removed.)")
                    verification.not_found = True
            timings["verify"] = round(time.time() - t, 3)

        timings["total"] = round(time.time() - t0, 3)
        cited = set()
        for c in verification.claims:
            cited.update(c.citations)
        cited.update(int(n) for n in re.findall(r"\[(\d+)\]", final))
        yield {"type": "final", "answer": final, "verification": verification.summary(),
               "sources": [dict(s.to_dict(), cited=s.n in cited) for s in registry.items], "timings": timings}

    # ------------------------------------------------------------ verification
    def _verify(self, answer: str, registry: SourceRegistry, question: str,
                known_ok: Optional[set] = None) -> Verification:
        v = Verification(claims=split_claims(answer), not_found=is_not_found(answer))
        deterministic_check(v.claims, registry, question)
        if self.cfg.answer.judge:
            todo = [c for c in v.claims if c.status == "ok" and c.effective_section == "documented"
                    and not (known_ok and c.text in known_ok)]
            if todo:
                v.judge_used = True
                self._judge(todo, registry)
        return v

    def _judge(self, claims: list[Claim], registry: SourceRegistry) -> None:
        size = max(1, self.cfg.answer.judge_batch)
        batches = [claims[i:i + size] for i in range(0, len(claims), size)]

        def run(batch: list[Claim]):
            items = [{"id": c.id, "claim": c.text,
                      "sources": [(n, (registry.get(n).prompt_text() if registry.get(n) else "")[:JUDGE_SOURCE_CHARS])
                                  for n in c.citations if registry.get(n)]} for c in batch]
            res = self.llm.chat([{"role": "system", "content": JUDGE_SYSTEM},
                                 {"role": "user", "content": judge_user_prompt(items)}],
                                json_mode=True, temperature=0.0, thinking=False, max_tokens=1500)
            return batch, parse_json_object(res.content) or {}

        with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
            futures = [pool.submit(run, b) for b in batches]
            for f in futures:
                try:
                    batch, obj = f.result()
                except Exception as exc:  # judge unavailable: keep deterministic result, say so
                    for c in claims:
                        c.judge = f"judge error: {exc.__class__.__name__}"
                    continue
                verdicts = {int(v.get("id", -1)): v for v in obj.get("verdicts", []) if isinstance(v, dict)
                            and str(v.get("id", "")).lstrip("-").isdigit()}
                for c in batch:
                    v = verdicts.get(c.id)
                    if not v:
                        c.judge = "no verdict"
                        continue
                    c.judge = str(v.get("verdict", "")).lower()
                    if c.judge in ("partial", "unsupported"):
                        c.status = "fail"
                        c.reasons.append(f"judge: {c.judge} - {v.get('reason', '')}".strip(" -"))

    def _regenerate(self, system: str, registry: SourceRegistry, plan: QueryPlan, draft: str,
                    verification: Verification, thinking: bool) -> str:
        sources = "\n\n---\n\n".join(s.prompt_text() for s in registry.items)
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": answer_user_prompt(plan.standalone, sources, [], "")},
                {"role": "assistant", "content": draft},
                {"role": "user", "content": REGENERATE_PROMPT.format(problems=verification.problems_text())}]
        try:
            return self.llm.chat(msgs, thinking=thinking).content.strip()
        except Exception:
            return ""

    def ask(self, question: str, history: Optional[Sequence[dict]] = None,
            domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
            top_k: Optional[int] = None) -> dict:
        out: dict = {"question": question, "tools": [], "draft": ""}
        for ev in self.ask_stream(question, history, domains, doc_ids, top_k):
            if ev["type"] == "plan":
                out["plan"], out["scope"], out["thinking"] = ev["plan"], ev["scope"], ev["thinking"]
            elif ev["type"] == "tool":
                out["tools"].append({k: ev[k] for k in ("name", "args", "summary")})
            elif ev["type"] == "draft":
                out["draft"] += ev["text"]
            elif ev["type"] == "draft_reset":
                out["draft"] = ""
            elif ev["type"] == "sources":
                out["confidence"] = ev["confidence"]
            elif ev["type"] == "final":
                out.update({k: ev[k] for k in ("answer", "verification", "sources", "timings")})
        return out
