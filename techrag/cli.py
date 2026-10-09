"""Command line interface: techrag <command> ..."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from techrag.config import Config, load_config


def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _engine(cfg: Config):
    from techrag.engine import RAGEngine

    return RAGEngine(cfg)


def _format_sources(sources) -> str:
    lines = []
    for p in sources:
        pages = f"s.{p.page_start}" if p.page_start == p.page_end else f"s.{p.page_start}-{p.page_end}"
        sec = f" — {p.section}" if p.section else ""
        lines.append(f"  [{p.number}] {p.doc_title}{sec} — {pages}  (skor {p.score:.3f}, {p.domain})")
    return "\n".join(lines)


# ---------------------------------------------------------------- commands

def cmd_ingest(cfg: Config, args) -> int:
    from techrag.domains import DomainRegistry
    from techrag.embeddings import create_embedder
    from techrag.ingest.pipeline import Ingestor
    from techrag.store import Store

    store = Store(cfg.db_path)
    embedder = create_embedder(cfg.embedding)
    ingestor = Ingestor(cfg, store, embedder, DomainRegistry.load(cfg.paths.domains_file), progress=_print)
    report = ingestor.run(Path(args.path) if args.path else None, rebuild=args.rebuild, prune=not args.no_prune)
    _print("\n" + report.summary())
    for path, err in report.failed.items():
        _print(f"  HATA {path}: {err}")
    return 1 if report.failed else 0


def cmd_inspect(cfg: Config, args) -> int:
    from techrag.ingest.pipeline import build_chunks
    from techrag.ingest.structure import SECTION_SEP

    doc, blocks, chunks = build_chunks(cfg, Path(args.file))
    _print(f"{doc.title}: {doc.n_pages} pages, {len(doc.toc)} outline entries, {len(blocks)} blocks, "
           f"{len(chunks)} chunks")
    for w in doc.warnings:
        _print(f"  warning: {w}")
    for c in chunks[args.start:args.start + args.limit]:
        _print(f"\n--- chunk {c.ordinal} [{c.kind}] p.{c.page_start}-{c.page_end} ~{c.tokens} tok")
        _print(f"    section: {SECTION_SEP.join(c.section) or '-'}")
        _print(c.text[: args.chars] + (" ..." if len(c.text) > args.chars else ""))
    return 0


def cmd_search(cfg: Config, args) -> int:
    engine = _engine(cfg)
    if args.no_rewrite:
        engine.planner.use_llm = False
    res = engine.retrieve(args.query, domains=args.domain, doc_ids=args.doc, top_k=args.k)
    plan = res.plan
    _print(f"Dil: {plan.language} | İngilizce: {plan.english}")
    if plan.keywords:
        _print(f"Anahtar kelimeler: {', '.join(plan.keywords)}")
    _print(f"Yönlendirilen koleksiyonlar: {', '.join(res.routed_domains) or 'tümü'} | "
           f"aday={res.candidates} | süreler={res.timings}")
    for p in res.passages:
        _print("\n" + _format_sources([p]))
        _print("    " + p.text[: args.chars].replace("\n", "\n    ") + (" ..." if len(p.text) > args.chars else ""))
    return 0


def _stream_answer(engine, question: str, history: list, args) -> Optional[str]:
    answer = ""
    sources = []
    for ev in engine.ask_stream(question, history, domains=args.domain, doc_ids=args.doc):
        if ev["type"] == "plan" and args.verbose:
            _print(f"(plan: {json.dumps(ev['plan'], ensure_ascii=False)})")
        elif ev["type"] == "sources":
            from techrag.retrieval import Passage

            sources = [Passage(**s) for s in ev["sources"]]
            conf = ev.get("confidence")
            if args.verbose and conf is not None:
                _print(f"(geri getirme güveni: {conf:.2f})")
        elif ev["type"] == "token":
            sys.stdout.write(ev["text"])
            sys.stdout.flush()
        elif ev["type"] == "replace":
            _print("\n\n[Doğrulama sonrası düzeltilmiş cevap]\n" + ev["text"])
        elif ev["type"] == "done":
            answer = ev["answer"]
            _print("\n")
            if sources:
                _print("Kaynaklar:\n" + _format_sources(sources))
            v = ev.get("verification")
            if v and v["status"] == "warning":
                _print("\n⚠ Doğrulama uyarıları (orijinal dokümandan kontrol edin):")
                if v["unsupported_numbers"]:
                    _print(f"  - kaynaklarda bulunamayan değerler: {', '.join(v['unsupported_numbers'])}")
                if v["invalid_citations"]:
                    _print(f"  - var olmayan kaynağa atıf: {', '.join(f'[{n}]' for n in v['invalid_citations'])}")
                if not v["citations_used"]:
                    _print("  - cevapta hiç [n] atfı yok")
            if args.verbose:
                _print(f"(süreler: {ev['timings']})")
    return answer


def cmd_ask(cfg: Config, args) -> int:
    engine = _engine(cfg)
    if args.json:
        result = engine.ask(args.question, domains=args.domain, doc_ids=args.doc)
        _print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    _stream_answer(engine, args.question, [], args)
    return 0


def cmd_chat(cfg: Config, args) -> int:
    engine = _engine(cfg)
    history: list[dict] = []
    _print("techrag sohbet — çıkmak için /q, geçmişi silmek için /reset")
    while True:
        try:
            q = input("\nSoru> ").strip()
        except (EOFError, KeyboardInterrupt):
            _print()
            return 0
        if not q:
            continue
        if q in ("/q", "/quit", "/exit"):
            return 0
        if q == "/reset":
            history.clear()
            _print("(geçmiş silindi)")
            continue
        _print()
        try:
            answer = _stream_answer(engine, q, history, args)
        except Exception as exc:
            _print(f"\nHATA: {exc}")
            continue
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": answer or ""}]


def cmd_stats(cfg: Config, args) -> int:
    from techrag.store import Store

    _print(json.dumps(Store(cfg.db_path).stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_docs(cfg: Config, args) -> int:
    from techrag.store import Store

    for d in Store(cfg.db_path).documents():
        warn = f"  ⚠ {len(d.warnings)} uyarı" if d.warnings else ""
        _print(f"{d.id:>4}  [{d.domain:<11}] {d.title}  ({d.n_pages} s., {d.n_chunks} parça){warn}")
    return 0


def cmd_eval(cfg: Config, args) -> int:
    from techrag.evaluation import load_items, run_eval, save_report

    engine = _engine(cfg)
    if args.no_rewrite:
        engine.planner.use_llm = False
    items = load_items(args.file)
    if args.only:
        items = [i for i in items if any(i.id.startswith(o) for o in args.only)]
    report = run_eval(engine, items, retrieval_only=args.retrieval_only, progress=_print)
    _print("\n" + json.dumps(report["summary"], ensure_ascii=False, indent=2))
    path = save_report(report, args.out)
    _print(f"Rapor: {path}")
    return 0


def cmd_doctor(cfg: Config, args) -> int:
    from techrag.llm import LLMClient
    from techrag.store import Store

    ok = True

    def check(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        _print(f"[{'OK ' if good else 'XX '}] {name}{': ' + detail if detail else ''}")

    check("offline mode", os.environ.get("HF_HUB_OFFLINE") == "1", "HF_HUB_OFFLINE=" + os.environ.get("HF_HUB_OFFLINE", ""))
    check("sources dir", cfg.sources_dir.exists(), str(cfg.sources_dir))
    check("domains file", Path(cfg.paths.domains_file).exists(), cfg.paths.domains_file)
    stats = Store(cfg.db_path).stats()
    check("index", stats["chunks"] > 0, f"{stats['documents']} documents, {stats['chunks']} chunks")

    if cfg.embedding.backend in ("sentence_transformers", "st", "local"):
        check("embedding model dir", Path(cfg.embedding.model).exists(), cfg.embedding.model)
    try:
        from techrag.embeddings import create_embedder

        emb = create_embedder(cfg.embedding)
        dim = emb.embed_queries(["test"]).shape[1]
        check("embedding model loads", True, f"{emb.name}, dim={dim}")
        if stats["embedding_model"]:
            check("index matches embedder", stats["embedding_model"] == emb.name,
                  f"index={stats['embedding_model']} config={emb.name}")
    except Exception as exc:
        check("embedding model loads", False, str(exc))

    if cfg.reranker.enabled:
        try:
            from techrag.reranker import create_reranker

            rr = create_reranker(cfg.reranker)
            s = rr.score("PCIe link training", ["The LTSSM controls link training.", "Banana bread recipe."])
            check("reranker loads", s[0] > s[1], f"scores={[round(x, 3) for x in s]}")
        except Exception as exc:
            check("reranker loads", False, str(exc))
    else:
        _print("[-- ] reranker disabled")

    h = LLMClient(cfg.llm).health()
    check("LLM server", h["ok"], f"{cfg.llm.provider} {cfg.llm.base_url} model={cfg.llm.model}"
          + (f" -> {h['error']}" if h.get("error") else ""))
    if not h["ok"] and h.get("models"):
        _print(f"      available models: {', '.join(h['models'][:20])}")
    return 0 if ok else 1


def cmd_serve(cfg: Config, args) -> int:
    import uvicorn

    from techrag.server import create_app

    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    _print(f"techrag web arayüzü: http://{host}:{port}")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info")
    return 0


# ------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="techrag", description="Offline source-grounded RAG for interface standards")
    p.add_argument("-c", "--config", help="config file (default: config/config.yaml or $TECHRAG_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="index documents under the sources dir (incremental)")
    s.add_argument("path", nargs="?", help="file or folder (default: paths.sources_dir)")
    s.add_argument("--rebuild", action="store_true", help="re-index even unchanged files")
    s.add_argument("--no-prune", action="store_true", help="keep index entries whose files were deleted")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("inspect", help="show how a file is parsed/chunked (nothing is indexed)")
    s.add_argument("file")
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--chars", type=int, default=800)
    s.set_defaults(func=cmd_inspect)

    def scope(sp):
        sp.add_argument("-d", "--domain", action="append", help="restrict to collection(s), e.g. -d pcie")
        sp.add_argument("--doc", action="append", type=int, help="restrict to document id(s)")

    s = sub.add_parser("search", help="retrieval only: show the passages a question would get")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=None)
    s.add_argument("--chars", type=int, default=500)
    s.add_argument("--no-rewrite", action="store_true", help="skip LLM query planning")
    scope(s)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("ask", help="answer one question")
    s.add_argument("question")
    s.add_argument("--json", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    scope(s)
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("chat", help="interactive chat with follow-up questions")
    s.add_argument("-v", "--verbose", action="store_true")
    scope(s)
    s.set_defaults(func=cmd_chat)

    s = sub.add_parser("serve", help="start the web UI / API")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    sub.add_parser("stats", help="index statistics").set_defaults(func=cmd_stats)
    sub.add_parser("docs", help="list indexed documents").set_defaults(func=cmd_docs)
    sub.add_parser("doctor", help="check models, LLM server and index").set_defaults(func=cmd_doctor)

    s = sub.add_parser("eval", help="run an evaluation question set")
    s.add_argument("file", nargs="?", default="eval/questions.yaml")
    s.add_argument("--retrieval-only", action="store_true", help="measure retrieval without calling the LLM")
    s.add_argument("--no-rewrite", action="store_true")
    s.add_argument("--only", action="append", help="only item ids starting with this prefix")
    s.add_argument("--out", default="data/eval_reports")
    s.set_defaults(func=cmd_eval)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    try:
        return args.func(cfg, args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if os.environ.get("TECHRAG_DEBUG"):
            raise
        print(f"HATA: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
