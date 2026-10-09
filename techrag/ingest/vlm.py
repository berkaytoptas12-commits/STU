"""Eager, filtered VLM table extraction.

1. Score every page cheaply (captions, Min/Max/Typ/Unit headers, number+unit density, ruled tables).
2. Render only pages above the threshold and send image + text layer to the vision model, which returns
   the tables (merged cells resolved, continuation tables, footnotes) and typed parameter rows.
3. Ground deterministically against the PDF text layer (techrag.geometry): every value must sit in the
   row of its parameter/row label AND under its column header (and column group), with its unit and
   conditions printed on the page. The VLM's structure is only a hypothesis; a value that cannot be tied
   to exactly one cell, sits under another header (min/max swapped) or has another unit is not verified.
   Unverified rows stay out of answers; unverified tables become search-only hints and the original PDF
   text of the table is kept.
4. Cache per (document hash, page, model, prompt version) so re-ingestion and shared libraries never
   pay for the same page twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from techrag.geometry import FIELD_HEADERS, PageGeom, find_unit, ground_cell, norm_tokens
from techrag.llm import LLMClient, image_part, parse_json_object
from techrag.units import (NON_SI_UNITS, base_unit, find_quantities, number_ambiguous, to_si, unit_info,
                           value_kind)

PROMPT_VERSION = "vlm-tables-v1"

_CAPTION = re.compile(r"^\s*(?:table|tablo)\s+[A-Z]?[\d.\-–]+", re.I | re.M)
_FIGURE = re.compile(r"^\s*(?:figure|fig\.|şekil)\s+[A-Z]?[\d.\-–]+", re.I | re.M)
_HINTS = re.compile(r"\b(min(?:imum)?|max(?:imum)?|typ(?:ical)?|nom(?:inal)?|parameter|symbol|units?|"
                    r"conditions?|notes?|description|value|default|range)\b", re.I)
_NUM = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

SYSTEM = """You extract tables from one page of a technical standard (hardware interface / avionics / electronics).
You receive the page image and the page's text layer. The image shows the table STRUCTURE; the text layer has the EXACT characters.

Return ONLY a JSON object:
{"tables": [
  {"caption": "Table 4-12 ... (exact caption text, add '(continued)' if the table continues from a previous page)",
   "columns": ["column header", ...],
   "rows": [["cell", ...], ...],
   "footnotes": ["1. footnote text", ...],
   "parameters": [
     {"parameter": "name as written", "symbol": "", "min": "", "typ": "", "max": "", "unit": "",
      "conditions": "", "notes": ""}
   ]}
]}

Rules:
- Copy every value exactly as it appears in the text layer (same digits, decimal separators, symbols). Never compute, convert, round or guess a value.
- Resolve merged/spanned cells by repeating their value in every row/column they cover.
- Multi-level headers: join them with " / " (e.g. "DDR4-3200 / Min").
- "parameters": one entry per measurable quantity with limits/values. When values are given per speed grade, mode, device density or other column group, emit one entry per group and put the group (e.g. "DDR4-3200", "Fast-mode", "8Gb") in "conditions" together with any test conditions of the row.
- Put the full text of footnotes referenced by a row into its "notes". Keep "unit" exactly as written (ns, mV, MHz, tCK, UI, %, ...).
- Use "" for missing fields. Skip register/bit-field tables in "parameters" (keep them in rows).
- If the page has no real table, return {"tables": []}. Do not describe figures."""


@dataclass
class ExtractedTable:
    page: int
    caption: str
    columns: list[str]
    rows: list[list[str]]
    footnotes: list[str] = field(default_factory=list)
    parameters: list[dict] = field(default_factory=list)
    verified_ratio: float = 0.0
    status: str = "unchecked"   # grounded | partial | conflict | no_text_layer | unchecked
    grounding: dict = field(default_factory=dict)

    @property
    def grounded(self) -> bool:
        return self.status == "grounded"

    @property
    def markdown(self) -> str:
        width = max([len(self.columns)] + [len(r) for r in self.rows]) if (self.columns or self.rows) else 0
        if width == 0:
            return ""
        cols = (self.columns + [""] * width)[:width] if self.columns else [f"col{i + 1}" for i in range(width)]
        lines = [self.caption] if self.caption else []
        lines += ["| " + " | ".join(_cell(c) for c in cols) + " |", "|" + "---|" * width]
        for r in self.rows:
            r = (list(r) + [""] * width)[:width]
            lines.append("| " + " | ".join(_cell(c) for c in r) + " |")
        if self.footnotes:
            lines.append("Notes: " + " ".join(_cell(f) for f in self.footnotes))
        return "\n".join(lines)


def _cell(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).replace("|", "/").strip()


# ------------------------------------------------------------------------------ page selection

def page_table_score(text: str, drawings: int = 0, pdf_tables: int = 0) -> float:
    score = 3.0 * min(len(_CAPTION.findall(text)), 2)
    score += min(len({m.lower()[:3] for m in _HINTS.findall(text)}), 6) * 0.75
    score += min(len(find_quantities(text)) / 4.0, 5.0)
    score += 3.0 * min(pdf_tables, 1)
    if drawings >= 20:
        score += 1.0
    return score


def is_figure_page(text: str, drawings: int, images: int) -> bool:
    return bool(_FIGURE.search(text)) and (images > 0 or drawings >= 30)


def select_pages(page_texts: dict[int, str], page_stats: dict, min_score: float, max_pages: int) -> list[int]:
    scored = []
    for page, text in page_texts.items():
        st = page_stats.get(page)
        s = page_table_score(text, getattr(st, "drawings", 0), getattr(st, "pdf_tables", 0))
        if s >= min_score:
            scored.append((s, page))
    scored.sort(reverse=True)
    return sorted(p for _, p in scored[:max_pages])


# ------------------------------------------------------------------------------ validation

def _norm_text(text: str) -> str:
    return (text.replace("−", "-").replace("–", "-").replace(" ", " ")
            .replace(" ", " ").replace(" ", " "))


def value_in_text(value: str, text: str) -> bool:
    """Every number inside `value` occurs in `text` as a whole number token."""
    nums = _NUM.findall(_norm_text(value or ""))
    if not nums:
        return True
    hay = _norm_text(text)
    for n in nums:
        n = n.lstrip("+")
        pat = r"(?<![\d.,])" + re.escape(n) + r"(?![\d])(?![.,]\d)"
        if not re.search(pat, hay):
            alt = n.replace(",", ".") if "," in n else n.replace(".", ",")
            if not re.search(r"(?<![\d.,])" + re.escape(alt) + r"(?![\d])(?![.,]\d)", hay):
                return False
    return True


# A table replaces the PDF-extracted table only when (nearly) every cell is tied to its row and column.
MIN_GROUNDED = 0.98
_LETTER = re.compile(r"[A-Za-z]")


def _row_labels(row: list[str]) -> list[str]:
    """Leading cells that name the row (parameter, symbol, condition) rather than hold a value."""
    out = []
    for c in row[:3]:
        if c and _LETTER.search(c) and value_kind(c) in ("expression", "empty"):
            out.append(c)
        elif out:
            break
    return out


def _present(geom: PageGeom, text: str) -> bool:
    return bool(geom.find_phrase(text)) or geom.locate_text(text).status in ("exact", "approximate", "ambiguous")


def ground_table(table: ExtractedTable, geom: Optional[PageGeom], prev: Optional[PageGeom] = None) -> None:
    """Cell-level grounding of a whole VLM table (sets status, verified_ratio, grounding)."""
    if geom is None:
        table.status, table.verified_ratio = "unchecked", 0.0
        table.grounding = {"reason": "no page geometry"}
        return
    if not geom.has_text:
        table.status, table.verified_ratio = "no_text_layer", 0.0
        table.grounding = {"reason": "page has no text layer (scanned)"}
        return
    counts = {"cells": 0, "grounded": 0, "conflict": 0, "ambiguous": 0, "not_found": 0}
    reasons: list[str] = []
    boxes = []
    for r in table.rows:
        labels = _row_labels(r)
        for j, cell in enumerate(r):
            if not cell or cell in labels or value_kind(cell) == "empty":
                continue
            counts["cells"] += 1
            if not re.search(r"\d", cell):
                ok = _present(geom, cell)
                counts["grounded" if ok else "not_found"] += 1
                if not ok:
                    reasons.append(f"'{cell}' not in the text layer")
                continue
            levels = [x.strip() for x in (table.columns[j] if j < len(table.columns) else "").split(" / ") if x.strip()]
            if not levels or not labels:
                counts["not_found"] += 1
                reasons.append(f"'{cell}' has no row label or column header")
                continue
            res = ground_cell(geom, cell, labels, [levels[-1]], levels[:-1], prev)
            key = res.status if res.status in counts else "not_found"
            counts[key] += 1
            if res.status != "grounded":
                reasons.append(res.reason)
            else:
                boxes += [res.value, res.label]
    total = counts["cells"]
    table.verified_ratio = round(counts["grounded"] / total, 3) if total else 0.0
    if counts["conflict"]:
        table.status = "conflict"
    elif total and table.verified_ratio >= MIN_GROUNDED:
        table.status = "grounded"
    else:
        table.status = "partial"
    table.grounding = dict(counts, reasons=reasons[:12])
    if boxes:
        from techrag.geometry import union

        table.grounding["bbox"] = union(boxes).rect()


def _conditions(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[;,]|\band\b", text or "") if len(norm_tokens(c)) >= 1 and c.strip()]


def _item(role: str, page: int, box, geom: Optional[PageGeom] = None, **kw) -> dict:
    if geom is not None and "text" not in kw:
        kw["text"] = " ".join(geom.words[i].text for i in box.words if i < len(geom.words))
    return dict(role=role, page=page, rect=box.rect(), **kw)


def ground_parameter(p: dict, geom: Optional[PageGeom], prev: Optional[PageGeom] = None,
                     nxt: Optional[PageGeom] = None, caption: str = "") -> None:
    """Relation-level check of one typed parameter row: each value under its min/typ/max header, in the row of
    the parameter (or of its condition), with its unit and conditions on the page. Sets verified/status/
    evidence/value_kind and the SI values (only for plain numbers with a known unit)."""
    unit = p.get("unit", "")
    fields = [(k, p.get(k, "")) for k in ("min", "typ", "max") if value_kind(p.get(k, "")) != "empty"]
    kinds = {value_kind(v) for _, v in fields}
    p["value_kind"] = "expression" if "expression" in kinds else ("range" if "range" in kinds else "number")
    flags: list[str] = []
    if unit and not unit_info(unit) and unit.strip().lower() not in NON_SI_UNITS:
        flags.append(f"unit '{unit}' has no known SI conversion")
    for k, v in fields:
        if value_kind(v) == "expression":
            flags.append(f"{k} is an expression, kept verbatim")
        elif number_ambiguous(v):
            flags.append(f"{k} '{v}': number format ambiguous (decimal or thousands separator)")
    for k in ("min", "typ", "max"):
        p[f"{k}_si"] = to_si(p.get(k, ""), unit)
    p["base_unit"] = base_unit(unit)
    evidence: dict = {"page": geom.page if geom else 0, "items": [], "reasons": [], "flags": flags,
                      "notes_located": not p.get("notes")}
    p["evidence"] = evidence
    p["verified"] = False
    if geom is None:
        p["status"] = "unchecked"
        evidence["reasons"].append("no page geometry")
        return
    if not geom.has_text:
        p["status"] = "no_text_layer"
        evidence["reasons"].append("page has no text layer")
        return
    if not fields:
        p["status"] = "no_value"
        return
    conds = _conditions(p.get("conditions", ""))
    missing = [c for c in conds if not _present(geom, c) and c.lower() not in caption.lower()]
    statuses: list[str] = []
    if missing:
        statuses.append("condition_not_found")
        evidence["reasons"].append(f"condition(s) not on the page: {missing}")
    names = [x for x in (p.get("symbol", ""), p.get("parameter", "")) if x]
    label_items: dict = {}
    for k, v in fields:
        res = ground_cell(geom, v, names, FIELD_HEADERS[k], conds, prev)
        if res.status == "not_found" and conds:
            res = ground_cell(geom, v, conds, FIELD_HEADERS[k], conds, prev, label_left=False)
        if res.status != "grounded":
            statuses.append(res.status)
            evidence["reasons"].append(f"{k}: {res.reason or res.status}")
            continue
        evidence["items"].append(_item("value", geom.page, res.value, field=k, text=v))
        if res.header is not None:
            hg = geom if (res.header_page or geom.page) == geom.page else prev
            evidence["items"].append(_item("header", res.header_page or geom.page, res.header, hg, field=k))
        if res.label is not None:
            label_items[tuple(res.label.rect())] = res.label
        for g in res.groups:
            evidence["items"].append(_item("condition", geom.page, g, geom))
        if unit:
            ustate, ubox = find_unit(geom, unit, res.value, res.header if res.header_page == geom.page else None)
            if ustate == "mismatch":
                statuses.append("unit_mismatch")
                evidence["reasons"].append(f"{k}: the row's unit is '{geom.words[ubox.words[0]].text}', not '{unit}'")
            elif ustate == "not_found":
                statuses.append("unit_not_found")
                evidence["reasons"].append(f"{k}: unit '{unit}' not printed in the row or column header")
            elif ubox is not None:
                evidence["items"].append(_item("unit", geom.page, ubox, text=unit))
    for box in label_items.values():
        evidence["items"].append(_item("label", geom.page, box, geom))
    if p.get("notes"):
        for g in (geom, nxt):
            if g is None or not g.has_text:
                continue
            m = g.locate_text(" ".join(norm_tokens(p["notes"])[:14]), min_cov=0.85)
            if m.status in ("exact", "approximate"):
                for b in g.line_boxes(m.words):
                    evidence["items"].append(_item("footnote", g.page, b, g))
                evidence["notes_located"] = True
                break
        if not evidence["notes_located"]:
            evidence["reasons"].append("footnote text not found in the text layer (left out of the cited row)")
    order = ["conflict", "ambiguous", "unit_mismatch", "condition_not_found", "not_found", "unit_not_found"]
    bad = [st for st in order if st in statuses]
    p["status"] = bad[0] if bad else "grounded"
    p["verified"] = p["status"] == "grounded"


def validate(table: ExtractedTable, geom: Optional[PageGeom], prev: Optional[PageGeom] = None,
             nxt: Optional[PageGeom] = None) -> ExtractedTable:
    ground_table(table, geom, prev)
    for p in table.parameters:
        ground_parameter(p, geom, prev, nxt, table.caption)
    return table


def _parse(obj: Optional[dict], page: int) -> list[ExtractedTable]:
    out = []
    for t in (obj or {}).get("tables", []) or []:
        if not isinstance(t, dict):
            continue
        rows = [[_cell(c) for c in r] for r in t.get("rows", []) if isinstance(r, list)]
        cols = [_cell(c) for c in t.get("columns", []) or []]
        if not rows and not cols:
            continue
        params = []
        for p in t.get("parameters", []) or []:
            if isinstance(p, dict) and (p.get("parameter") or p.get("symbol")):
                params.append({k: _cell(p.get(k, "")) for k in
                               ("parameter", "symbol", "min", "typ", "max", "unit", "conditions", "notes")})
        out.append(ExtractedTable(page, _cell(t.get("caption", "")), cols, rows,
                                  [_cell(f) for f in t.get("footnotes", []) or []], params))
    return out


# ------------------------------------------------------------------------------ extraction

class TableExtractor:
    def __init__(self, client: LLMClient, cache_dir: Path, dpi: int = 150, max_tokens: int = 6000,
                 concurrency: int = 4):
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.dpi = dpi
        self.max_tokens = max_tokens
        self.concurrency = max(1, concurrency)

    def _cache_file(self, doc_sha: str, page: int) -> Path:
        key = hashlib.sha256(f"{PROMPT_VERSION}|{self.client.model}|{self.dpi}".encode()).hexdigest()[:12]
        return self.cache_dir / doc_sha[:24] / f"p{page:05d}_{key}.json"

    def extract_page(self, pdf_path: Path, doc_sha: str, page: int, page_text: str,
                     read_only_cache: bool = False, geoms: Optional[dict] = None) -> list[ExtractedTable]:
        cache = self._cache_file(doc_sha, page)
        if cache.exists():
            obj = json.loads(cache.read_text(encoding="utf-8"))
        else:
            png = render_page(pdf_path, page, self.dpi)
            text_layer = page_text if page_text.strip() else "(no text layer: scanned page)"
            res = self.client.chat(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": [
                     {"type": "text", "text": f"Page {page}. Text layer:\n<<<\n{text_layer[:12000]}\n>>>"},
                     image_part(png)]}],
                json_mode=True, max_tokens=self.max_tokens, temperature=0.0, thinking=False)
            obj = parse_json_object(res.content) or {"tables": [], "_unparsed": res.content[:2000]}
            if not read_only_cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        geoms = geoms or {}
        return [validate(t, geoms.get(page), geoms.get(page - 1), geoms.get(page + 1)) for t in _parse(obj, page)]

    def extract(self, pdf_path: Path, doc_sha: str, pages: list[int], page_texts: dict[int, str],
                progress: Callable[[str], None] = lambda m: None,
                geoms: Optional[dict] = None) -> tuple[dict[int, list[ExtractedTable]], list[str]]:
        results: dict[int, list[ExtractedTable]] = {}
        errors: list[str] = []

        def run(page: int):
            try:
                return page, self.extract_page(pdf_path, doc_sha, page, page_texts.get(page, ""), geoms=geoms), None
            except Exception as exc:
                return page, [], f"p.{page}: VLM extraction failed ({exc.__class__.__name__}: {str(exc)[:160]})"

        done = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for page, tables, err in pool.map(run, pages):
                done += 1
                results[page] = tables
                if err:
                    errors.append(err)
                if done % 10 == 0 or done == len(pages):
                    progress(f"    VLM tables: {done}/{len(pages)} pages")
        return results, errors


def render_page(pdf_path: Path, page: int, dpi: int = 150) -> bytes:
    import pymupdf

    with pymupdf.open(str(pdf_path)) as doc:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("png")
