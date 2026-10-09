"""Eager, filtered VLM table extraction.

1. Score every page cheaply (captions, Min/Max/Typ/Unit headers, number+unit density, ruled tables).
2. Render only pages above the threshold and send image + text layer to the vision model, which returns
   the tables (merged cells resolved, continuation tables, footnotes) and typed parameter rows.
3. Validate deterministically: every numeric value must occur in the page's text layer. Rows that fail
   are kept but marked unverified; tables mostly unverified fall back to the PDF text.
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

from techrag.llm import LLMClient, image_part, parse_json_object
from techrag.units import base_unit, find_quantities, to_si

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


def validate(table: ExtractedTable, page_text: str) -> ExtractedTable:
    cells = [c for r in table.rows for c in r if _NUM.search(str(c or ""))]
    if cells and page_text.strip():
        table.verified_ratio = sum(value_in_text(str(c), page_text) for c in cells) / len(cells)
    elif not cells:
        table.verified_ratio = 1.0
    for p in table.parameters:
        vals = [p.get(k, "") for k in ("min", "typ", "max")]
        p["verified"] = bool(page_text.strip()) and all(value_in_text(v, page_text) for v in vals) \
            and any(_NUM.search(v or "") for v in vals)
        unit = p.get("unit", "")
        for k in ("min", "typ", "max"):
            v = p.get(k, "")
            p[f"{k}_si"] = to_si(_NUM.search(_norm_text(v)).group(0), unit) if _NUM.search(_norm_text(v or "")) else None
        p["base_unit"] = base_unit(unit)
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
                     read_only_cache: bool = False) -> list[ExtractedTable]:
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
        return [validate(t, page_text) for t in _parse(obj, page)]

    def extract(self, pdf_path: Path, doc_sha: str, pages: list[int], page_texts: dict[int, str],
                progress: Callable[[str], None] = lambda m: None) -> tuple[dict[int, list[ExtractedTable]], list[str]]:
        results: dict[int, list[ExtractedTable]] = {}
        errors: list[str] = []

        def run(page: int):
            try:
                return page, self.extract_page(pdf_path, doc_sha, page, page_texts.get(page, "")), None
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
