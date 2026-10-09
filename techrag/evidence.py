"""Evidence locations: where on the PDF page the text or table cell behind a claim's citation is.

Location and verification are separate results. A region says WHERE the cited text sits on the page;
the claim's verification status says WHETHER that text supports the claim. Locations come only from the
text-layer word positions stored at ingestion (no VLM call, no OCR guess); when the supporting text cannot
be tied to exactly one place, no region is returned and the status says why.

A location record (per claim and cited source):
    {"n": 3, "kind": "passage", "status": "located" | "ambiguous" | "not_located" | "no_text_layer" |
     "legacy_index" | "not_pdf" | "not_applicable",
     "quotes": ["the supporting sentence/row as in the source text"],
     "regions": [{"doc_id", "doc_sha256", "page", "page_label", "role", "rects": [[x0, y0, x1, y1] 0..1 of the
                  rendered page], "pdf_rects": [[...] PDF points], "quote", "match": "exact" | "approximate"}],
     "inputs": [...]}   # calculations: the inputs and where each of them is located
"""

from __future__ import annotations

import re
from typing import Optional

from techrag.geometry import PageGeom, ground_cell, norm_tokens
from techrag.units import find_quantities
from techrag.verify import CITATION, _bare_numbers, split_sentences

_STOP = set("""a an and are as at be by for from in is it of on or the to with this that these those shall should
may must can will its their there which who what when where how than then also ve veya ile bir bu için
olarak de da göre olan""".split())
_SEP = re.compile(r"^\|?\s*:?-{2,}")


def _numbers(text: str) -> set[str]:
    text = CITATION.sub(" ", text)
    nums = {q.number.lstrip("±+-").replace(",", ".") for q in find_quantities(text)}
    nums |= {n.replace(",", ".") for n in _bare_numbers(text)}
    return nums


def _content(text: str) -> set[str]:
    return {t for t in norm_tokens(CITATION.sub(" ", text)) if t not in _STOP and len(t) > 1 and not t[0].isdigit()}


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def units_of(text: str) -> list[dict]:
    """Source text -> sentences (lines of a paragraph re-joined) and table rows (each row knows its table's
    header row)."""
    out: list[dict] = []
    header: Optional[list[str]] = None
    para: list[str] = []

    def flush():
        if para:
            for sent in split_sentences(" ".join(para)):
                if len(sent.split()) >= 2:
                    out.append({"kind": "text", "text": sent})
            para.clear()

    lines = text.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            flush()
            header = None
            continue
        if s.startswith("|"):
            flush()
            if _SEP.match(s):
                continue
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if _SEP.match(nxt):
                header = _cells(s)
                continue
            out.append({"kind": "row", "text": s, "cells": _cells(s), "header": header or []})
            continue
        header = None
        para.append(s)
    flush()
    return out


def support_units(claim: str, source_text: str, limit: int = 3) -> list[dict]:
    """The smallest parts of the source that carry the claim: the best-scoring sentence/row, plus more only
    when the claim's numbers are spread over several of them."""
    nums, words = _numbers(claim), _content(claim)
    scored = []
    for u in units_of(source_text):
        un, uw = _numbers(u["text"]), _content(u["text"])
        hit_n = nums & un
        hit_w = len(words & uw) / max(1, len(words))
        if nums and not hit_n:
            continue
        if not nums and hit_w < 0.4:
            continue
        scored.append((3 * len(hit_n) + hit_w, u, hit_n))
    scored.sort(key=lambda x: -x[0])
    chosen, covered = [], set()
    for score, u, hit_n in scored:
        if chosen and (not nums or hit_n <= covered):
            continue
        chosen.append(dict(u, numbers=sorted(hit_n)))
        covered |= hit_n
        if len(chosen) >= limit or (nums and covered >= nums) or not nums:
            break
    return chosen


class EvidenceLocator:
    def __init__(self, store, catalog):
        self.store = store
        self.catalog = catalog

    def _region(self, geom: PageGeom, doc, word_ids, role: str, quote: str, match: str = "exact") -> dict:
        boxes = geom.line_boxes(word_ids)
        return {"doc_id": doc.id, "doc_sha256": doc.sha256, "page": geom.page, "page_label": geom.label,
                "role": role, "rects": [geom.to_display(b.rect()) for b in boxes],
                "pdf_rects": [geom.to_pdf(b.rect()) for b in boxes], "quote": quote, "match": match}

    def _geoms(self, doc, first: int, last: int) -> tuple[list[PageGeom], str]:
        if doc is None:
            return [], "not_located"
        if doc.legacy or not self.store.has_geometry:
            return [], "legacy_index"
        if not str(doc.path).lower().endswith(".pdf"):
            return [], "not_pdf"
        geoms = [g for p in range(first, max(first, last) + 1) if (g := self.store.page_geom(doc.id, p))]
        if not geoms:
            return [], "not_located"
        if not any(g.has_text for g in geoms):
            return geoms, "no_text_layer"
        return geoms, ""

    # ------------------------------------------------------------------ public
    def locate(self, claim_text: str, src) -> dict:
        rec = {"n": src.n, "kind": src.kind, "status": "not_located", "quotes": [], "regions": []}
        if src.kind == "user":
            rec["status"] = "not_applicable"
            return rec
        if src.kind == "calc":
            rec["status"] = "not_applicable"
            rec["inputs"] = src.extra.get("inputs", [])
            return rec
        doc = self.catalog.docs().get(src.doc_id) if src.doc_id is not None else None
        if src.kind == "parameter":
            return self._parameter(rec, src, doc)
        units = support_units(claim_text, src.evidence())
        rec["quotes"] = [u["text"] for u in units]
        geoms, problem = self._geoms(doc, src.page_start, src.page_end)
        if problem:
            rec["status"] = problem
            return rec
        if not units:
            rec["status"] = "not_located"
            return rec
        states = []
        for u in units:
            st, regions = (self._row(u, geoms, doc) if u["kind"] == "row" else self._text(u, geoms, doc))
            states.append(st)
            rec["regions"] += regions
        rec["status"] = "located" if rec["regions"] and all(s == "located" for s in states) else (
            "located" if rec["regions"] else ("ambiguous" if "ambiguous" in states else "not_located"))
        if rec["regions"] and any(s != "located" for s in states):
            rec["partial"] = True
        return rec

    def locate_value(self, value: str, src) -> dict:
        """Where a calculation input (a number) sits in its source."""
        rec = self.locate(value, src)
        return rec

    # ----------------------------------------------------------------- helpers
    def _text(self, u: dict, geoms: list[PageGeom], doc) -> tuple[str, list[dict]]:
        best = None
        amb = False
        for g in geoms:
            m = g.locate_text(u["text"])
            if m.status in ("exact", "approximate") and (best is None or m.score > best[1].score):
                best = (g, m)
            elif m.status == "ambiguous":
                amb = True
        if best is None:
            return ("ambiguous" if amb else "not_located"), []
        g, m = best
        return "located", [self._region(g, doc, m.words, "text", u["text"], m.status)]

    def _row(self, u: dict, geoms: list[PageGeom], doc) -> tuple[str, list[dict]]:
        cells, header = u["cells"], u["header"]
        labels = []
        for c in cells[:3]:
            if c and re.search(r"[A-Za-z]", c) and not re.fullmatch(r"[-+±]?\d+(?:[.,]\d+)*\s*\w{0,3}", c):
                labels.append(CITATION.sub("", c).strip())
            elif labels:
                break
        want = set(u.get("numbers") or [])
        regions, state = [], "not_located"
        for j, cell in enumerate(cells):
            cell = CITATION.sub("", cell).strip()
            if not cell or cell in labels or not re.search(r"\d", cell):
                continue
            if want and not (_numbers(cell) & want):
                continue
            head = [x.strip() for x in (header[j] if j < len(header) else "").split(" / ") if x.strip()]
            if not head or not labels:
                continue
            value = cell
            nums = [q.number for q in find_quantities(cell)] or _bare_numbers(cell)
            for g in geoms:
                res = ground_cell(g, value, labels, [head[-1]], head[:-1])
                if res.status == "not_found" and nums and nums[0] != value:
                    value = nums[0]
                    res = ground_cell(g, value, labels, [head[-1]], head[:-1])
                if res.status == "grounded":
                    regions.append(self._region(g, doc, res.value.words, "value", cell))
                    if res.label is not None:
                        regions.append(self._region(g, doc, res.label.words, "label", " ".join(labels)))
                    if res.header is not None and res.header_page == g.page:
                        regions.append(self._region(g, doc, res.header.words, "header", head[-1]))
                    state = "located"
                    break
                if res.status == "ambiguous" and state != "located":
                    state = "ambiguous"
        if regions:
            return "located", _dedupe(regions)
        # Fall back to the row as a text sequence (PDF tables whose header could not be matched).
        st, regs = self._text({"text": " ".join(CITATION.sub("", c) for c in cells)}, geoms, doc)
        for r in regs:
            r["role"] = "row"
        return (st if regs else state), regs

    def _parameter(self, rec: dict, src, doc) -> dict:
        ev = src.extra.get("evidence") or {}
        items = ev.get("items") or []
        if not items:
            rec["status"] = "legacy_index" if (doc and doc.legacy) else "not_located"
            return rec
        for it in items:
            g = self.store.page_geom(doc.id, it["page"]) if doc else None
            if g is None:
                continue
            r = it["rect"]
            rec["regions"].append({"doc_id": doc.id, "doc_sha256": doc.sha256, "page": it["page"],
                                   "page_label": g.label, "role": it["role"], "rects": [g.to_display(r)],
                                   "pdf_rects": [g.to_pdf(r)], "quote": it.get("text", ""), "match": "exact"})
        rec["quotes"] = [src.text]
        rec["status"] = "located" if rec["regions"] else "not_located"
        return rec


def _dedupe(regions: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in regions:
        key = (r["page"], r["role"], tuple(tuple(x) for x in r["rects"]))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out
