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

from techrag.config import write_text_atomic
from techrag.geometry import FIELD_HEADERS, PageGeom, find_unit, ground_cell, norm_tokens
from techrag.llm import LLMClient, image_part, parse_json_object
from techrag.units import (NON_SI_UNITS, base_unit, find_quantities, number_ambiguous, to_si, unit_info,
                           value_kind)

PROMPT_VERSION = "vlm-tables-v1"

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


def _read_cache(path: Path) -> Optional[dict]:
    """A cached page result, or None (not cached yet, or an unreadable leftover: the page is asked again)."""
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _cell(v) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "")).replace("|", "/").strip()


# ------------------------------------------------------------------------------ page selection

# A table caption starts its line and is followed by a title, a dash/colon or nothing - not by running text
# ("Table 4-2 shows ..." in a wrapped paragraph is a reference, not a caption).
_CAPTION_START = re.compile(r"^\s*(?:table|tablo)\s+[A-Z]?\d+(?:[.\-–]\d+)*[a-z]?\s*", re.I)


def table_captions(text: str) -> int:
    n = 0
    for line in text.splitlines():
        m = _CAPTION_START.match(line)
        if not m:
            continue
        rest = line[m.end():]
        if not rest or rest[0] in "—–:.-(" or rest[0].isupper() or rest[0].isdigit():
            n += 1
    return n


def _rendered_words(geom) -> list[tuple[float, float, float, float]]:
    """Word boxes as the page is shown (rotation applied); sideways text is turned so that lines run along x."""
    a, b, c, d, e, f = geom.matrix
    boxes = []
    for w in geom.words:
        xs = (a * w.x0 + c * w.y0 + e, a * w.x1 + c * w.y1 + e)
        ys = (b * w.x0 + d * w.y0 + f, b * w.x1 + d * w.y1 + f)
        boxes.append((min(xs), min(ys), max(xs), max(ys), len(w.text)))
    tall = sum(1 for x0, y0, x1, y1, n in boxes if n >= 3 and (y1 - y0) > 1.5 * (x1 - x0))
    wide = sum(1 for x0, y0, x1, y1, n in boxes if n >= 3 and (x1 - x0) > (y1 - y0))
    if tall > wide:   # a table printed sideways on the page
        return [(y0, x0, y1, x1) for x0, y0, x1, y1, _ in boxes]
    return [(x0, y0, x1, y1) for x0, y0, x1, y1, _ in boxes]


def aligned_rows(geom, min_cells: int = 3) -> int:
    """Text lines that sit in a grid of columns shared with other lines: a table without ruling lines.

    A line is split into cells where the gap between two words is wider than about one line height (word
    spacing in running text is a fraction of that). Each cell's left edge, centre and right edge are anchors;
    an anchor used by three or more lines is a column. A line counts when at least three of its cells stand on
    a column. Running text, also in two-column layouts, has no such lines."""
    if geom is None or len(getattr(geom, "words", ()) or ()) < 9:
        return 0
    lines: list[list] = []
    for x0, y0, x1, y1 in sorted(_rendered_words(geom), key=lambda b: ((b[1] + b[3]) / 2, b[0])):
        h, cy = max(y1 - y0, 1.0), (y0 + y1) / 2
        if lines and abs(cy - lines[-1][0]) <= 0.45 * h:
            lines[-1][1].append((x0, x1, h))
        else:
            lines.append([cy, [(x0, x1, h)]])
    rows: list[list[tuple[float, float]]] = []
    for _, words in lines:
        words.sort()
        hs = sorted(w[2] for w in words)
        gap = max(hs[len(hs) // 2] * 1.0, 6.0)
        cells = [[words[0][0], words[0][1]]]
        for x0, x1, _ in words[1:]:
            if x0 - cells[-1][1] > gap:
                cells.append([x0, x1])
            else:
                cells[-1][1] = max(cells[-1][1], x1)
        if len(cells) >= min_cells:
            rows.append([(c0, c1) for c0, c1 in cells])
    if len(rows) < 3:
        return 0
    support: dict[tuple[int, int], set[int]] = {}
    for i, cells in enumerate(rows):
        for c0, c1 in cells:
            for kind, x in ((0, c0), (1, (c0 + c1) / 2), (2, c1)):
                b = int(round(x / 4.0))
                for k in (b - 1, b, b + 1):
                    support.setdefault((kind, k), set()).add(i)
    count = 0
    for i, cells in enumerate(rows):
        on_column = 0
        for c0, c1 in cells:
            if any(len(support.get((kind, int(round(x / 4.0))), ())) >= 3
                   for kind, x in ((0, c0), (1, (c0 + c1) / 2), (2, c1))):
                on_column += 1
        if on_column >= min_cells:
            count += 1
    return count if count >= 3 else 0


def table_signal(text: str, pdf_tables: int = 0, aligned: int = 0) -> bool:
    """A page goes to the VLM only with a sign of a real table: a table caption, a table found by the PDF table
    detector, or text aligned in columns. Values and words like 'min/max' in running text are not enough."""
    return bool(pdf_tables) or aligned >= 3 or table_captions(text) > 0


def page_table_score(text: str, drawings: int = 0, pdf_tables: int = 0, aligned: int = 0) -> float:
    """Cheap score that ranks table pages; 0 for a page without a table signal (see table_signal)."""
    if not table_signal(text, pdf_tables, aligned):
        return 0.0
    score = 3.0 * min(table_captions(text), 2)
    score += min(len({m.lower()[:3] for m in _HINTS.findall(text)}), 6) * 0.75
    score += min(len(find_quantities(text)) / 4.0, 5.0)
    score += 3.0 * min(pdf_tables, 1)
    if aligned >= 3:
        score += 3.0 + min(aligned / 4.0, 2.0)
    if drawings >= 20:
        score += 1.0
    return score


def is_figure_page(text: str, drawings: int, images: int) -> bool:
    return bool(_FIGURE.search(text)) and (images > 0 or drawings >= 30)


def page_candidates(page_texts: dict[int, str], page_stats: dict, min_score: float,
                    geoms: Optional[dict] = None) -> list[int]:
    """Pages with a table signal whose cheap table score reaches the threshold, best first."""
    scored = []
    for page, text in page_texts.items():
        st = page_stats.get(page)
        pdf_tables = getattr(st, "pdf_tables", 0)
        aligned = 0
        if geoms and not pdf_tables:
            try:
                aligned = aligned_rows(geoms.get(page))
            except Exception:   # a page geometry that cannot be read only loses this signal
                aligned = 0
        s = page_table_score(text, getattr(st, "drawings", 0), pdf_tables, aligned)
        if s >= min_score:
            scored.append((s, page))
    scored.sort(reverse=True)
    return [p for _, p in scored]


def select_pages(page_texts: dict[int, str], page_stats: dict, min_score: float, max_pages: int,
                 geoms: Optional[dict] = None) -> list[int]:
    return sorted(page_candidates(page_texts, page_stats, min_score, geoms)[:max_pages])


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
        rows = [[_cell(c) for c in r] for r in t.get("rows") or [] if isinstance(r, list)]
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
        self.failed: dict[int, str] = {}

    def _cache_file(self, doc_sha: str, page: int) -> Path:
        key = hashlib.sha256(f"{PROMPT_VERSION}|{self.client.model}|{self.dpi}".encode()).hexdigest()[:12]
        return self.cache_dir / doc_sha[:24] / f"p{page:05d}_{key}.json"

    def extract_page(self, pdf_path: Path, doc_sha: str, page: int, page_text: str,
                     read_only_cache: bool = False, geoms: Optional[dict] = None) -> list[ExtractedTable]:
        cache = self._cache_file(doc_sha, page)
        obj = _read_cache(cache)
        if obj is None:
            png = render_page(pdf_path, page, self.dpi)
            text_layer = page_text if page_text.strip() else "(no text layer: scanned page)"
            res = self.client.chat(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": [
                     {"type": "text", "text": f"Page {page}. Text layer:\n<<<\n{text_layer[:12000]}\n>>>"},
                     image_part(png)]}],
                json_mode=True, max_tokens=self.max_tokens, temperature=0.0, thinking=False)
            obj = parse_json_object(res.content)
            if res.finish_reason == "length" or not isinstance(obj, dict) or not isinstance(obj.get("tables"), list):
                # A cut-off or unreadable reply is not "no tables on this page": report it, cache nothing.
                why = "the reply was cut off (raise vision.max_tokens)" if res.finish_reason == "length" \
                    else "the reply is not the expected JSON"
                raise ValueError(f"page {page}: {why}; re-index the document to retry")
            if not read_only_cache:
                write_text_atomic(cache, json.dumps(obj, ensure_ascii=False))
        geoms = geoms or {}
        return [validate(t, geoms.get(page), geoms.get(page - 1), geoms.get(page + 1)) for t in _parse(obj, page)]

    def cached(self, doc_sha: str, page: int) -> bool:
        return _read_cache(self._cache_file(doc_sha, page)) is not None

    def extract(self, pdf_path: Path, doc_sha: str, pages: list[int], page_texts: dict[int, str],
                progress: Callable[[str], None] = lambda m: None,
                geoms: Optional[dict] = None) -> tuple[dict[int, list[ExtractedTable]], list[str]]:
        """Tables per page and one error line per failed page; `self.failed` then maps each failed page to its
        reason (a failed page is never reported as a page without tables)."""
        results: dict[int, list[ExtractedTable]] = {}
        errors: list[str] = []
        self.failed: dict[int, str] = {}

        def run(page: int):
            try:
                return page, self.extract_page(pdf_path, doc_sha, page, page_texts.get(page, ""), geoms=geoms), None
            except Exception as exc:
                return page, [], f"{exc.__class__.__name__}: {str(exc)[:160]}"

        done = 0
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            for page, tables, err in pool.map(run, pages):
                done += 1
                results[page] = tables
                if err:
                    self.failed[page] = err
                    errors.append(f"p.{page}: VLM extraction failed ({err})")
                if done % 10 == 0 or done == len(pages):
                    progress(f"    VLM tables: {done}/{len(pages)} pages")
        return results, errors


def render_page(pdf_path: Path, page: int, dpi: int = 150) -> bytes:
    import pymupdf

    with pymupdf.open(str(pdf_path)) as doc:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        return pix.tobytes("png")
