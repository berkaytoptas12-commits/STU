"""The answering engine.

plan (LLM) -> scope (named standards hard-filtered; a standard that is not loaded is reported, ambiguous
scope asks back) -> scoped retrieval + verified parameter rows + figure pages -> tool-using agent loop
(streamed draft = progress only) -> verification (deterministic + strict judge) -> one regeneration ->
answer rebuilt from verified claims -> evidence locations for the citation highlights.

Events from ask_stream():
  plan, sources, sources_add, status, reasoning, tool, draft, draft_reset, clarify, final, error
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
from techrag.evidence import EvidenceLocator
from techrag.ingest.metadata import AMENDING_TYPES
from techrag.ingest.vlm import render_page as _render
from techrag.llm import LLMClient, ToolsUnsupported, image_part, parse_json_object
from techrag.prompts import (JUDGE_SYSTEM, NOT_FOUND, REGENERATE_PROMPT, answer_system_prompt,
                             answer_user_prompt, judge_user_prompt, message)
from techrag.query import QueryPlan, QueryPlanner
from techrag.reranker import Reranker, create_reranker
from techrag.retrieval import DocCatalog, RetrievalResult, Retriever, Scope
from techrag.store import Store
from techrag.tools import TOOL_SCHEMAS, SourceRegistry, ToolExecutor, parameter_conflicts
from techrag.verify import (CITATION, NOT_CHECKED, SUPPORTED, UNSUPPORTED, UNVERIFIED, Claim, ParsedAnswer,
                            Verification, apply_verdict, deterministic_check, is_not_found, judge_failed,
                            parse_answer, parse_citations, parse_verdicts, render)

_UNSET = object()
PAGE_DESCRIBE_PROMPT = ("Transcribe this page of a technical standard for an engineer. For figures, timing diagrams, "
                        "waveforms, pinouts and state diagrams: give the figure title, every label, signal/pin name, "
                        "timing parameter and value exactly as printed, and how the elements relate (which edge "
                        "a parameter is measured between, state transitions, pin numbering). Do not infer anything "
                        "that is not visible.")
MAX_FIGURES = 2
JUDGE_SOURCE_CHARS = 6000
_CLAUSE_REF = re.compile(r"(?:section|clause|§|sec\.)\s*(\d+(?:\.\d+)+)", re.I)


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
        return self.store.source_path(doc, self.cfg.sources_dir)

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
                 top_k: Optional[int] = None, buckets: Optional[Sequence[str]] = None) -> RetrievalResult:
        t = time.time()
        plan = self.planner.plan(question, history)
        plan_time = time.time() - t
        self.check_index_compatibility()
        res = self.retriever.search(plan, domains=domains, doc_ids=doc_ids, top_k=top_k, buckets=buckets)
        res.timings["plan"] = round(plan_time, 3)
        return res

    # --------------------------------------------------------------- relations
    def doc_meta(self, doc_id: int) -> dict:
        """Entities/type/revision + the precedence note shown in a source header."""
        docs = self.catalog.docs()
        d = docs.get(doc_id)
        if not d:
            return {}
        notes = []
        if d.doc_type in AMENDING_TYPES:
            targets = [docs[i] for i in self.catalog.amends(d.id) if i in docs]
            if targets:
                notes.append("Amends: " + "; ".join(f"'{t.title}'" + (f" rev {t.revision}" if t.revision else "")
                                                    for t in targets)
                             + " - only the clauses it addresses; for those it takes precedence")
            else:
                notes.append("Amended document not identified in the library - check applicability")
        if d.superseded_by and d.superseded_by in docs:
            n = docs[d.superseded_by]
            notes.append(f"SUPERSEDED revision (newer: '{n.title}'{' rev ' + n.revision if n.revision else ''})")
        fixes = [docs[i] for i in self.catalog.amended_by(d.id) if i in docs]
        if fixes:
            notes.append("Errata/ECN loaded for this document: " + "; ".join(f"'{f.title}'" for f in fixes))
        return {"entities": list(d.entities), "doc_type": d.doc_type, "revision": d.revision,
                "relation": " | ".join(notes)}

    def _link_clauses(self, registry: SourceRegistry) -> None:
        """Tie errata/ECN passages to the base-document passages of the clauses they address."""
        for e in registry.items:
            if e.doc_id is None or e.doc_type not in AMENDING_TYPES:
                continue
            targets = set(self.catalog.amends(e.doc_id))
            for ref in dict.fromkeys(_CLAUSE_REF.findall(e.text)):
                for b in registry.items:
                    if b.doc_id in targets and re.search(r"(?:^|> )" + re.escape(ref) + r"\b", b.section or ""):
                        note_b = f"errata [{e.n}] addresses §{ref}"
                        if note_b not in b.relation:
                            b.relation = f"{b.relation} | {note_b}".strip(" |")
                        note_e = f"addresses §{ref} of [{b.n}]"
                        if note_e not in e.relation:
                            e.relation = f"{e.relation} | {note_e}".strip(" |")

    # ------------------------------------------------------------------ answer
    def _thinking(self, plan: QueryPlan) -> bool:
        mode = self.cfg.llm.thinking
        return mode == "on" or (mode == "auto" and plan.needs_reasoning)

    def _scope_note(self, s: Scope, conflicts: Sequence[str] = ()) -> str:
        lines = []
        if s.reason == "entity":
            lines.append(f"Search scope: only documents tagged {', '.join(s.entities)} (named in the question). "
                         f"The tools stay inside this scope.")
        elif s.reason == "user":
            lines.append("Search scope: restricted to the documents the user selected.")
        if s.missing:
            lines.append(f"NOT LOADED: no document for {', '.join(s.missing)}. Say so; never answer about it from "
                         f"another standard's or version's sources.")
        for n in s.notes:
            lines.append(f"Scope note: {n}.")
        if conflicts:
            lines.append("CONFLICTING VALUES between sources (state the conflict with both citations, do not merge): "
                         + "; ".join(conflicts))
        return ("\n".join(lines) + "\n\n") if lines else ""

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

    def _related_docs(self, scope: Scope) -> list[str]:
        doms = {self.domains.entity_domain(e) for e in scope.missing} - {None}
        out = []
        for d in self.catalog.docs().values():
            if d.domain in doms:
                out.append(f"{d.title}" + (f" [{', '.join(d.entities)}]" if d.entities else ""))
        return out[:8]

    def _clarification(self, plan: QueryPlan, res: RetrievalResult, user_scoped: bool) -> Optional[dict]:
        """Ask back when no standard is named but the evidence for a value question comes from sibling
        standards/versions of one collection (whose values may differ)."""
        if not self.cfg.retrieval.clarify_ambiguous or user_scoped or plan.entities or res.scope.reason != "all" \
                and res.scope.reason != "domain":
            return None
        if not (plan.parameters or plan.question_type == "numeric"):
            return None
        docs = self.catalog.docs()
        by_domain: dict[str, dict[str, list[str]]] = {}
        doc_ids = [p.doc_id for p in res.passages[:5]] + [r.doc_id for r in res.parameters[:6]]
        for i in doc_ids:
            d = docs.get(i)
            if d and d.entities and d.doc_type == "base":
                for e in d.entities:
                    by_domain.setdefault(d.domain, {}).setdefault(e, [])
                    if d.title not in by_domain[d.domain][e]:
                        by_domain[d.domain][e].append(d.title)
        for dom, ents in by_domain.items():
            if len(ents) >= 2:
                lang = plan.language
                opts = [{"label": f"{e} — {', '.join(t[:2])}", "question": f"{plan.question} ({e})"}
                        for e, t in sorted(ents.items())]
                opts.append({"label": message(lang, "clarify_all"),
                             "question": f"{plan.question} ({' vs '.join(sorted(ents))})"})
                text = message(lang, "clarify", options=", ".join(sorted(ents)))
                return {"text": text, "options": opts, "entities": sorted(ents)}
        return None

    def _final_short(self, answer: str, status: str, timings: dict, scope: Scope, **extra) -> dict:
        v = Verification(enabled=self.cfg.answer.verify, judge_enabled=self.cfg.answer.judge, status=status,
                         not_found=status == "not_found", clarify=status == "clarify")
        return {"type": "final", "answer": answer, "verification": v.summary(), "sources": [], "timings": timings,
                "cite_claims": [], "scope": scope.to_dict(), **extra}

    def ask_stream(self, question: str, history: Optional[Sequence[dict]] = None,
                   domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
                   top_k: Optional[int] = None, buckets: Optional[Sequence[str]] = None) -> Iterator[dict]:
        t0 = time.time()
        history = list(history or [])
        yield {"type": "status", "stage": "planning"}
        res = self.retrieve(question, history, domains, doc_ids, top_k, buckets)
        plan = res.plan
        lang = plan.language
        scope = res.scope
        thinking = self._thinking(plan)
        yield {"type": "plan", "plan": plan.to_dict(), "scope": scope.to_dict(), "thinking": thinking,
               "planner_error": None if plan.rewritten_by_llm else self.planner.last_error}
        timings = dict(res.timings)

        if self.store.stats()["chunks"] == 0:
            yield self._final_short(f"{NOT_FOUND[lang]} {message(lang, 'empty_library')}", "not_found", timings, scope)
            return
        if scope.reason == "entity_missing":
            missing = ", ".join(scope.missing)
            if scope.buckets or doc_ids:
                # Loaded, but not in what the user selected: say so instead of "not loaded".
                outside = [f"{d.title}" for d in self.catalog.docs().values() if not d.missing and
                           {e.lower() for e in d.entities} & {m.lower() for m in scope.missing}][:6]
                text = f"{NOT_FOUND[lang]}\n\n{message(lang, 'missing_scope', missing=missing)}"
                if outside:
                    text += " " + message(lang, "outside_scope", docs="; ".join(outside))
                yield self._final_short(text, "not_found", timings, scope)
                return
            related = self._related_docs(scope)
            text = f"{NOT_FOUND[lang]}\n\n{message(lang, 'missing', missing=missing)}"
            if related:
                text += " " + message(lang, "missing_loaded", loaded="; ".join(related))
            yield self._final_short(text, "not_found", timings, scope)
            return
        clar = self._clarification(plan, res, bool(doc_ids or domains or buckets))
        if clar:
            yield {"type": "clarify", **clar}
            yield self._final_short(clar["text"], "clarify", timings, scope, clarify=clar)
            return

        registry = SourceRegistry()
        for p in res.passages:
            registry.add_passage(p, self.doc_meta(p.doc_id))
        for r in res.parameters:
            registry.add_parameter(r, self.doc_meta(r.doc_id))

        # Figures referenced by top passages go to the (multimodal) model as page images.
        images = []
        if self.cfg.vision.enabled:
            for p in res.passages:
                if not p.figure_page or len(images) >= MAX_FIGURES:
                    continue
                page_text = "\n".join(c.text for c in self.store.page_chunks(p.doc_id, p.figure_page))[:4000]
                if self.chat_sees_images:
                    png = self.render_page(p.doc_id, p.figure_page)
                    if png:
                        src, _ = registry.add_page(p.doc_id, p.figure_page, p.doc_title,
                                                   "(page image attached)\n" + page_text, self.doc_meta(p.doc_id),
                                                   evidence_text=page_text, image=True)
                        images.append((src.n, png))
                else:
                    desc = self.describe_page(p.doc_id, p.figure_page)
                    if desc:
                        registry.add_page(p.doc_id, p.figure_page, p.doc_title,
                                          f"{desc}\n\nPage text (text layer):\n{page_text}",
                                          self.doc_meta(p.doc_id), evidence_text=page_text)
        images = [(n, png) for n, png in images if png]
        user_src = registry.add_user_input(question if plan.standalone == question
                                           else f"{question}\n{plan.standalone}")
        self._link_clauses(registry)
        yield {"type": "sources", "sources": [s.to_dict() for s in registry.items], "confidence": res.confidence}

        system = answer_system_prompt(lang)
        hints = self.domains.glossary_hints(plan.domains, f"{plan.question} {plan.standalone}")
        note = self._scope_note(scope, parameter_conflicts(registry))
        user_text = answer_user_prompt(plan.standalone, "\n\n---\n\n".join(s.prompt_text() for s in registry.items),
                                       hints, note)
        if images:
            user_content: object = [{"type": "text", "text": user_text}] + [
                part for n, png in images for part in ({"type": "text", "text": f"Page image for source [{n}]:"},
                                                       image_part(png))]
        else:
            user_content = user_text
        messages = [{"role": "system", "content": system}, *self._history_messages(history),
                    {"role": "user", "content": user_content}]

        executor = ToolExecutor(self.store, self.retriever, registry, plan, self.render_page,
                                vision_input=self.chat_sees_images, scope=scope, expand=self.domains.expand_terms,
                                resolve_standard=self.domains.detect_entities, describe_page=self.describe_page,
                                doc_meta=self.doc_meta)
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
        t = time.time()
        if self.cfg.answer.verify and draft:
            yield {"type": "status", "stage": "verifying"}
        verification, parsed = self._verify(draft, registry)
        if verification.failing and self.cfg.answer.regenerate:
            yield {"type": "status", "stage": "regenerating", "failed": len(verification.failing)}
            revised = self._regenerate(system, registry, plan, draft, verification, thinking)
            if revised:
                known = {(c.text, tuple(c.citations)): (c.judge["verdict"], c.judge["reason"])
                         for c in verification.claims if c.status == SUPPORTED}
                yield {"type": "status", "stage": "verifying"}
                v2, parsed2 = self._verify(revised, registry, known)
                v2.regenerated = True
                v2.judge_called = v2.judge_called or verification.judge_called
                if len(v2.failing) <= len(verification.failing):
                    draft, verification, parsed = revised, v2, parsed2
        final = self._compose(verification, parsed, draft, lang)
        if self.cfg.answer.verify:
            timings["verify"] = round(time.time() - t, 3)

        t = time.time()
        self._locate(verification.claims, registry)
        timings["locate"] = round(time.time() - t, 3)
        timings["total"] = round(time.time() - t0, 3)
        cited = {n for c in verification.claims if c.outcome in ("kept", "flagged") for n in c.citations}
        cited.update(int(n) for n in re.findall(r"\[(\d+)\]", final))
        sources = []
        for s in registry.items:
            d = dict(s.to_dict(), cited=s.n in cited)
            if s.doc_id is not None:
                g = self.store.page_geom(s.doc_id, s.page_start)
                d["page_label"] = g.label if g else ""
            sources.append(d)
        yield {"type": "final", "answer": final, "verification": verification.summary(), "sources": sources,
               "timings": timings, "cite_claims": cite_claims(final, verification.claims), "scope": scope.to_dict(),
               "user_input": user_src.n if user_src else None}

    # ------------------------------------------------------------ verification
    def _verify(self, answer: str, registry: SourceRegistry,
                known: Optional[dict] = None) -> tuple[Verification, ParsedAnswer]:
        parsed = parse_answer(answer)
        v = Verification(claims=parsed.claims, not_found=is_not_found(answer), enabled=self.cfg.answer.verify,
                         judge_enabled=self.cfg.answer.judge)
        if not v.enabled:
            for c in v.claims:
                c.status = NOT_CHECKED
            return v, parsed
        deterministic_check(v.claims, registry)
        pending = [c for c in v.claims if c.status == "pending"]
        if not self.cfg.answer.judge:
            for c in pending:
                c.status = NOT_CHECKED  # numbers/citations passed; the semantic check is switched off
            return v, parsed
        todo = []
        for c in pending:
            c.judge["required"] = True
            key = (c.text, tuple(c.citations))
            if known and key in known:
                apply_verdict(c, *known[key])
            else:
                todo.append(c)
        if todo:
            v.judge_called = True
            self._judge(todo, registry, v)
        v.judge_completed = bool(pending) and all(c.judge["completed"] for c in pending)
        return v, parsed

    def _judge_batch(self, batch: list[Claim], registry: SourceRegistry) -> dict:
        items = []
        for c in batch:
            srcs = []
            for n in dict.fromkeys(c.citations):
                s = registry.get(n)
                if s is not None:
                    srcs.append((n, f"{s.header()}\n{s.evidence()}"[:JUDGE_SOURCE_CHARS]))
            items.append({"id": c.id, "type": c.kind, "claim": c.text, "sources": srcs})
        res = self.llm.chat([{"role": "system", "content": JUDGE_SYSTEM},
                             {"role": "user", "content": judge_user_prompt(items)}],
                            json_mode=True, temperature=0.0, thinking=False, max_tokens=1500)
        return parse_verdicts(parse_json_object(res.content), [c.id for c in batch])

    def _judge(self, claims: list[Claim], registry: SourceRegistry, v: Verification) -> None:
        """Strict: only an explicit 'supported' for the claim's id approves it. Anything else (error, timeout,
        malformed output, missing/duplicate id, unknown verdict) leaves it unverified; such claims get one retry."""
        size = max(1, self.cfg.answer.judge_batch)
        for attempt in range(2):
            todo = claims if attempt == 0 else [c for c in claims if not c.judge["completed"]
                                                and c.status != UNSUPPORTED]
            if not todo:
                return
            batches = [todo[i:i + size] for i in range(0, len(todo), size)]
            with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
                futures = [(b, pool.submit(self._judge_batch, b, registry)) for b in batches]
                for batch, fut in futures:
                    try:
                        verdicts = fut.result()
                    except Exception as exc:
                        err = f"judge error: {exc.__class__.__name__}: {str(exc)[:160]}"
                        v.judge_errors.append(err)
                        for c in batch:
                            judge_failed(c, err)
                        continue
                    for c in batch:
                        verdict, reason = verdicts[c.id]
                        if verdict not in ("supported", "partial", "unsupported"):
                            v.judge_errors.append(f"claim {c.id}: {reason}")
                        apply_verdict(c, verdict, reason)

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

    def _compose(self, v: Verification, parsed: ParsedAnswer, draft: str, lang: str) -> str:
        """The final answer is built from the claims: supported ones stay, unsupported/unverified ones are
        removed (or flagged when the user chose 'flag'). Sets v.status."""
        flag = self.cfg.answer.failed_claims == "flag"

        def decide(c: Claim) -> str:
            if c.status in (SUPPORTED, NOT_CHECKED):
                return "keep"
            return "flag" if flag else "drop"

        text = render(parsed, decide) if draft else ""
        for c in v.claims:
            d = decide(c)
            c.outcome = {"keep": "kept", "flag": "flagged", "drop": "removed"}[d]
        v.removed = [c.text for c in v.claims if c.outcome == "removed"]
        v.flagged = [c.text for c in v.claims if c.outcome == "flagged"]
        kept = [c for c in v.claims if c.outcome in ("kept", "flagged")]
        if not v.enabled:
            v.status = "not_checked"
            return text or NOT_FOUND[lang]
        if not v.claims:
            if not draft or v.not_found:
                v.status, v.not_found = "not_found", True
                return text if (text and is_not_found(text)) else NOT_FOUND[lang]
            v.status, v.parse_failed = "unverified", True
            return f"{NOT_FOUND[lang]}\n\n({message(lang, 'no_claims')})"
        if not kept:
            if any(c.status == UNVERIFIED for c in v.claims):
                v.status = "unverified"
                return message(lang, "incomplete")
            v.status, v.not_found = "not_found", True
            return f"{NOT_FOUND[lang]}\n\n{message(lang, 'removed_all')}"
        if all(c.status == SUPPORTED for c in kept):
            v.status = "corrected" if v.removed else "supported"
        elif any(c.outcome == "flagged" for c in kept):
            v.status = "warning"
        else:
            v.status = "not_checked"
        return text

    # ------------------------------------------------------------ evidence
    def _locate(self, claims: list[Claim], registry: SourceRegistry) -> None:
        locator = EvidenceLocator(self.store, self.catalog)
        for c in claims:
            c.evidence = []
            if c.outcome not in ("kept", "flagged"):
                continue
            for n in dict.fromkeys(c.citations):
                s = registry.get(n)
                if s is None:
                    continue
                try:
                    rec = locator.locate(c.text, s)
                    if s.kind == "calc":
                        rec["inputs"] = []
                        for inp in s.extra.get("inputs", []):
                            locs = []
                            for m in inp.get("sources", []):
                                src = registry.get(m)
                                if src is not None and src.kind not in ("calc", "user"):
                                    locs.append(locator.locate(inp["value"], src))
                                elif src is not None:
                                    locs.append({"n": m, "kind": src.kind, "status": "not_applicable",
                                                 "regions": [], "quotes": []})
                            rec["inputs"].append({"value": inp["value"], "sources": inp.get("sources", []),
                                                  "locations": locs})
                except Exception as exc:  # a location problem must never break the answer
                    rec = {"n": n, "kind": s.kind, "status": "not_located", "regions": [], "quotes": [],
                           "error": f"{exc.__class__.__name__}: {exc}"}
                c.evidence.append(rec)

    def ask(self, question: str, history: Optional[Sequence[dict]] = None,
            domains: Optional[Sequence[str]] = None, doc_ids: Optional[Sequence[int]] = None,
            top_k: Optional[int] = None, buckets: Optional[Sequence[str]] = None) -> dict:
        out: dict = {"question": question, "tools": [], "draft": ""}
        for ev in self.ask_stream(question, history, domains, doc_ids, top_k, buckets):
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
            elif ev["type"] == "clarify":
                out["clarify"] = {k: ev[k] for k in ("text", "options", "entities")}
            elif ev["type"] == "final":
                out.update({k: ev[k] for k in ("answer", "verification", "sources", "timings", "cite_claims")})
        return out


def cite_claims(answer: str, claims: list[Claim]) -> list[Optional[int]]:
    """For every citation button the UI renders (in text order, ranges expanded), the id of the claim it
    belongs to - so clicking [n] highlights the evidence of THAT statement."""
    spans = []
    pos = 0
    for c in claims:
        if c.outcome not in ("kept", "flagged"):
            continue
        i = answer.find(c.text, pos)
        if i < 0:
            i = answer.find(c.text)
        if i >= 0:
            spans.append((i, i + len(c.text), c.id))
            pos = i + len(c.text)
    out: list[Optional[int]] = []
    for m in CITATION.finditer(answer):
        cid = next((sid for a, b, sid in spans if a <= m.start() < b), None)
        out.extend([cid] * len(parse_citations(m.group(0))))
    return out
