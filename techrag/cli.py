"""Command line: techrag <command> ...   (the desktop exe accepts the same commands: TechRAG.exe ingest ...)"""

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


def _src_line(s: dict) -> str:
    if s.get("kind") == "calc":
        return f"  [{s['n']}] calc: {s['text']}"
    pages = f"p.{s['page_start']}" if s["page_start"] == s["page_end"] else f"p.{s['page_start']}-{s['page_end']}"
    std = f" [{', '.join(s.get('entities') or [])}]" if s.get("entities") else ""
    kind = f" ({s['kind']})" if s.get("kind") not in (None, "passage") else ""
    return f"  [{s['n']}]{std} {s['doc_title']} — {s.get('section') or '-'} — {pages}{kind}"


# --------------------------------------------------------------------------- commands

def cmd_ingest(cfg: Config, args) -> int:
    from techrag.ingest.pipeline import Ingestor

    if cfg.read_only:
        _print("The library is configured read-only.")
        return 2
    e = _engine(cfg)
    vision = None if args.no_vlm else e.vision
    llm = e.llm if (cfg.llm.model and not args.no_llm_meta) else None
    if vision is None and not args.no_vlm:
        _print("note: no vision model configured -> tables from PDF text only (Settings > Vision)")
    ing = Ingestor(cfg, e.store, e.embedder, e.domains, progress=_print, llm=llm, vision=vision)
    report = ing.run(Path(args.path) if args.path else None, rebuild=args.rebuild, prune=not args.no_prune)
    _print("\n" + report.summary())
    for path, err in report.failed.items():
        _print(f"  FAILED {path}: {err}")
    return 1 if report.failed else 0


def cmd_inspect(cfg: Config, args) -> int:
    from techrag.ingest.pipeline import build_chunks, page_texts
    from techrag.ingest.structure import SECTION_SEP
    from techrag.ingest.vlm import page_table_score

    doc, blocks, chunks = build_chunks(cfg, Path(args.file))
    _print(f"{doc.title}: {doc.n_pages} pages, {len(doc.toc)} outline entries, {len(blocks)} blocks, {len(chunks)} chunks")
    for w in doc.warnings:
        _print(f"  warning: {w}")
    if args.table_pages:
        texts = page_texts(blocks)
        scored = sorted(((page_table_score(texts[p], doc.page_stats.get(p).drawings if p in doc.page_stats else 0,
                                           doc.page_stats.get(p).pdf_tables if p in doc.page_stats else 0), p)
                         for p in texts), reverse=True)
        sel = [p for s, p in scored if s >= cfg.vision.min_page_score]
        _print(f"  VLM table pages (score >= {cfg.vision.min_page_score}): {len(sel)} -> {sorted(sel)[:60]}")
    for c in chunks[args.start:args.start + args.limit]:
        _print(f"\n--- chunk {c.ordinal} [{c.kind}] p.{c.page_start}-{c.page_end} ~{c.tokens} tok")
        _print(f"    section: {SECTION_SEP.join(c.section) or '-'}")
        _print(c.text[: args.chars] + (" ..." if len(c.text) > args.chars else ""))
    return 0


def cmd_search(cfg: Config, args) -> int:
    e = _engine(cfg)
    if args.no_rewrite:
        e.planner.use_llm = False
    res = e.retrieve(args.query, domains=args.domain, doc_ids=args.doc, top_k=args.k)
    p = res.plan
    _print(f"language={p.language} type={p.question_type} english={p.english!r}")
    _print(f"scope={res.scope.reason} entities={res.scope.entities} candidates={res.candidates} timings={res.timings}")
    for i, ps in enumerate(res.passages, 1):
        _print(f"\n[{i}] {', '.join(ps.entities)} {ps.doc_title} — {ps.section} — p.{ps.page_start}-{ps.page_end} "
               f"(score {ps.score:.3f}{', fig p.' + str(ps.figure_page) if ps.figure_page else ''})")
        _print("    " + ps.text[: args.chars].replace("\n", "\n    "))
    if res.parameters:
        _print("\nParameter rows:")
        for r in res.parameters:
            _print(f"  {r.doc_title} p.{r.page}: {r.line()}")
    return 0


def _stream(e, question: str, history: list, args) -> str:
    answer = ""
    for ev in e.ask_stream(question, history, domains=args.domain, doc_ids=args.doc):
        kind = ev["type"]
        if kind == "status" and args.verbose:
            _print(f"({ev['stage']})")
        elif kind == "plan" and args.verbose:
            _print(f"(scope: {ev['scope']}, thinking: {ev['thinking']}, english: {ev['plan']['english']!r})")
        elif kind == "tool":
            _print(f"  > {ev['name']}: {ev['summary']}")
        elif kind == "final":
            answer = ev["answer"]
            _print("\n" + answer + "\n")
            v = ev["verification"]
            _print(f"verification: {v['status']} — {v['supported']}/{v['checked']} statements supported"
                   + (f", {len(v['removed'])} removed" if v["removed"] else "")
                   + (" (regenerated once)" if v["regenerated"] else ""))
            for c in v["details"]:
                if c["status"] in ("removed", "fail"):
                    _print(f"  - removed: {c['text']}\n      reason: {'; '.join(c['reasons'])}")
            cited = [s for s in ev["sources"] if s.get("cited")]
            if cited:
                _print("Sources:")
                for s in cited:
                    _print(_src_line(s))
            if args.verbose:
                _print(f"(timings: {ev['timings']})")
        elif kind == "error":
            _print(f"ERROR: {ev['message']}")
    return answer


def cmd_ask(cfg: Config, args) -> int:
    e = _engine(cfg)
    if args.json:
        _print(json.dumps(e.ask(args.question, domains=args.domain, doc_ids=args.doc), ensure_ascii=False, indent=2))
        return 0
    _stream(e, args.question, [], args)
    return 0


def cmd_chat(cfg: Config, args) -> int:
    e = _engine(cfg)
    history: list[dict] = []
    _print("techrag chat — /q to quit, /reset to clear history")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if q in ("/q", "/quit", "/exit"):
            return 0
        if q == "/reset":
            history.clear()
            continue
        if not q:
            continue
        try:
            a = _stream(e, q, history, args)
        except Exception as exc:
            _print(f"ERROR: {exc}")
            continue
        history += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]


def cmd_stats(cfg: Config, args) -> int:
    from techrag.store import Store

    _print(json.dumps(Store(cfg.db_path, read_only=True).stats(), ensure_ascii=False, indent=2))
    return 0


def cmd_docs(cfg: Config, args) -> int:
    from techrag.store import Store

    for d in Store(cfg.db_path, read_only=True).documents():
        flags = []
        if d.doc_type != "base":
            flags.append(d.doc_type.upper())
        if d.superseded_by:
            flags.append(f"superseded by #{d.superseded_by}")
        if d.warnings:
            flags.append(f"{len(d.warnings)} warning(s)")
        _print(f"{d.id:>4} [{d.domain:<11}] {', '.join(d.entities) or '-':<16} {d.title} "
               f"(rev {d.revision or '-'}, {d.n_pages} p., {d.n_chunks} chunks) {' | '.join(flags)}")
    return 0


def cmd_publish(cfg: Config, args) -> int:
    from techrag.store import Store

    target = Store(cfg.db_path, read_only=cfg.read_only).publish(args.dest, cfg.sources_dir, cfg.vlm_cache_dir)
    _print(f"published to {target.parent}")
    return 0


def cmd_models(cfg: Config, args) -> int:
    from techrag.api import APIClient

    for name in ("llm", "vision", "embedding", "reranker"):
        svc = cfg.vision_service() if name == "vision" else getattr(cfg, name)
        try:
            models = APIClient(svc).list_models()
            _print(f"{name:<10} {svc.base_url}: {', '.join(models) or '(none)'}   selected: {svc.model or '-'}")
        except Exception as exc:
            _print(f"{name:<10} {svc.base_url}: ERROR {exc}")
    return 0


def cmd_cert(cfg: Config, args) -> int:
    """Show the certificate an HTTPS endpoint presents, whether the current trust settings accept it, and
    optionally trust it (saved under the settings folder and added to tls.ca_bundle)."""
    from techrag import tls
    from techrag.api import normalize_base_url
    from techrag.settings import settings_path, update_settings

    url = normalize_base_url(args.url)
    host, port, scheme = tls.host_port(url)
    if scheme != "https":
        _print(f"{url} is not https:// - no certificate involved.")
        return 0
    try:
        chain = tls.fetch_chain(host, port)
    except Exception as exc:
        code = tls.classify(exc)
        _print(f"cannot read the certificate of {host}:{port}: {exc}\n{tls.HINTS[code]}")
        return 1
    for i, der in enumerate(chain):
        info = tls.describe(der)
        _print(f"[{i}] subject : {info.subject}{'  (self-signed)' if info.self_signed else ''}")
        _print(f"    issuer  : {info.issuer}")
        _print(f"    names   : {', '.join(info.names) or '-'}")
        _print(f"    valid   : {info.not_before} -> {info.not_after}")
        _print(f"    sha256  : {info.sha256}")
    ok, code, msg = tls.check_handshake(host, port, tls.build_context(cfg.tls.system_store, cfg.tls.ca_bundle))
    _print(f"\nverification with current settings: {'OK' if ok else 'FAILED - ' + msg}")
    if not ok:
        _print(tls.HINTS[code])
    if args.save:
        _print(f"saved: {_save_to(chain, args.save)}")
    if args.trust and not ok:
        if code != tls.TLS_UNTRUSTED:
            _print("not trusting: the problem is not an unknown issuer (see the hint above).")
            return 1
        expected = tls.describe(chain[0]).sha256
        if not args.yes:
            answer = input(f"Trust {host}:{port} with fingerprint {expected}? [y/N] ").strip().lower()
            if answer not in ("y", "yes", "e", "evet"):
                return 1
        path = tls.save_chain(chain, settings_path().parent / "certs", host, port)
        files = [f for f in cfg.tls.ca_bundle.split(";") if f.strip()]
        if str(path) not in files:
            files.append(str(path))
        new_cfg = update_settings(cfg, {"tls": {"ca_bundle": ";".join(files)}})
        _print(f"trusted: {path} (added to settings tls.ca_bundle)")
        ok, _, msg = tls.check_handshake(host, port, tls.build_context(new_cfg.tls.system_store, new_cfg.tls.ca_bundle))
        _print(f"verification with the new settings: {'OK' if ok else 'FAILED - ' + msg}")
    return 0 if ok else 1


def _save_to(chain, target: str) -> str:
    import ssl

    Path(target).write_text("".join(ssl.DER_cert_to_PEM_cert(c) for c in chain), encoding="ascii")
    return target


def cmd_doctor(cfg: Config, args) -> int:
    from techrag.api import check_service
    from techrag.settings import settings_path

    ok = True

    def check(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        _print(f"[{'OK ' if good else 'XX '}] {name}{': ' + detail if detail else ''}")

    _print(f"settings file: {settings_path()}")
    from techrag.tls import ca_files

    _print(f"tls: windows/system store={'on' if cfg.tls.system_store else 'off'}, "
           f"extra CA files={len(ca_files(cfg.tls.ca_bundle))}, system proxy={'on' if cfg.tls.use_system_proxy else 'off'}")
    check("library", cfg.library_dir.exists(), f"{cfg.library_dir.resolve()} (read_only={cfg.read_only})")
    if cfg.db_path.exists():
        from techrag.store import Store

        st = Store(cfg.db_path, read_only=True).stats()
        check("index", st["chunks"] > 0, f"{st['documents']} docs, {st['chunks']} chunks, {st['parameters']} parameters, "
                                        f"embedding={st['embedding_model']}")
        if st["embedding_model"] and cfg.embedding.model:
            check("index matches embedding setting", st["embedding_model"] == f"api:{cfg.embedding.model}",
                  f"index={st['embedding_model']} setting=api:{cfg.embedding.model}")
    else:
        check("index", False, "not built yet (techrag ingest)")
    for name in ("llm", "vision", "embedding", "reranker"):
        svc = cfg.vision_service() if name == "vision" else getattr(cfg, name)
        if name == "reranker" and not cfg.reranker.enabled:
            _print("[-- ] reranker disabled")
            continue
        r = check_service(svc)
        check(f"{name} endpoint", r["ok"] and bool(svc.model),
              f"{svc.base_url} model={svc.model or '(not set)'}" + (f" -> {r['error']}" if r.get("error") else ""))
    return 0 if ok else 1


def cmd_serve(cfg: Config, args) -> int:
    import uvicorn

    from techrag.server import create_app

    host, port = args.host or cfg.server.host, args.port or cfg.server.port
    _print(f"techrag API + UI: http://{host}:{port}")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info")
    return 0


def cmd_eval(cfg: Config, args) -> int:
    from techrag.evaluation import load_items, run_eval, save_report

    e = _engine(cfg)
    if args.no_rewrite:
        e.planner.use_llm = False
    items = load_items(args.file)
    if args.only:
        items = [i for i in items if any(i.id.startswith(o) for o in args.only)]
    report = run_eval(e, items, retrieval_only=args.retrieval_only, progress=_print)
    _print("\n" + json.dumps(report["summary"], ensure_ascii=False, indent=2))
    _print(f"report: {save_report(report, args.out)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="techrag", description="Source-grounded assistant for interface and design standards")
    p.add_argument("-c", "--config", help="YAML config (default: ./config/config.yaml if present)")
    p.add_argument("--library", help="library folder (overrides settings)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="index the library's sources (incremental; VLM table extraction)")
    s.add_argument("path", nargs="?", help="file or folder (default: <library>/sources)")
    s.add_argument("--rebuild", action="store_true")
    s.add_argument("--no-prune", action="store_true")
    s.add_argument("--no-vlm", action="store_true", help="skip VLM table extraction")
    s.add_argument("--no-llm-meta", action="store_true", help="skip LLM metadata extraction")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("inspect", help="show parsing/chunking of a file (nothing is indexed)")
    s.add_argument("file")
    s.add_argument("--start", type=int, default=0)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--chars", type=int, default=700)
    s.add_argument("--table-pages", action="store_true", help="list pages that would go to the VLM")
    s.set_defaults(func=cmd_inspect)

    def scope(sp):
        sp.add_argument("-d", "--domain", action="append", help="restrict to collection(s)")
        sp.add_argument("--doc", action="append", type=int, help="restrict to document id(s)")

    s = sub.add_parser("search", help="retrieval only")
    s.add_argument("query")
    s.add_argument("-k", type=int)
    s.add_argument("--chars", type=int, default=400)
    s.add_argument("--no-rewrite", action="store_true")
    scope(s)
    s.set_defaults(func=cmd_search)

    for name, fn, hlp in (("ask", cmd_ask, "answer one question"), ("chat", cmd_chat, "interactive chat")):
        s = sub.add_parser(name, help=hlp)
        if name == "ask":
            s.add_argument("question")
            s.add_argument("--json", action="store_true")
        s.add_argument("-v", "--verbose", action="store_true")
        scope(s)
        s.set_defaults(func=fn)

    s = sub.add_parser("serve", help="run the API + UI in a browser-less server (headless/dev)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("publish", help="write a clean, shareable copy of the library")
    s.add_argument("dest")
    s.set_defaults(func=cmd_publish)

    sub.add_parser("stats", help="index statistics").set_defaults(func=cmd_stats)
    sub.add_parser("docs", help="list documents").set_defaults(func=cmd_docs)
    sub.add_parser("models", help="list models served by each configured endpoint").set_defaults(func=cmd_models)
    sub.add_parser("doctor", help="check endpoints, models and index").set_defaults(func=cmd_doctor)

    s = sub.add_parser("cert", help="inspect / trust the HTTPS certificate of a model endpoint")
    s.add_argument("url", help="e.g. https://vllm.company.local:8000/v1")
    s.add_argument("--save", help="write the presented chain to this .pem file")
    s.add_argument("--trust", action="store_true", help="trust it (asks for confirmation)")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    s.set_defaults(func=cmd_cert)

    s = sub.add_parser("eval", help="run a gold question set")
    s.add_argument("file", nargs="?", default="eval/questions.yaml")
    s.add_argument("--retrieval-only", action="store_true")
    s.add_argument("--no-rewrite", action="store_true")
    s.add_argument("--only", action="append")
    s.add_argument("--out", default="data/eval_reports")
    s.set_defaults(func=cmd_eval)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.library:
        cfg.paths.library_dir = args.library
    try:
        return args.func(cfg, args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if os.environ.get("TECHRAG_DEBUG"):
            raise
        print(f"ERROR: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
