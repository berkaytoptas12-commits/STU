"""Page geometry from the PDF text layer: extracted once at ingestion, stored in the index, and used for
(1) cell-level grounding of VLM tables and (2) locating the evidence of an answer on the page.

Coordinates
* Words are stored as PyMuPDF reports them: unrotated page space, origin at the CropBox top-left, points.
* ``PageGeom.to_display`` maps a rect to the rendered page (rotation and CropBox applied, origin top-left)
  normalised to 0..1 - the space of the page PNG, so an overlay stays aligned at any zoom, DPI or screen
  scaling.
* ``PageGeom.to_pdf`` maps a rect to PDF user space (unrotated, origin bottom-left), the stable reference.

Nothing here guesses a position: text that cannot be matched to text-layer words gets no location.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Optional, Sequence

_FOLD = str.maketrans({"µ": "u", "μ": "u", "Ω": " ohm ", "−": "-", "–": "-", "—": "-", "‐": "-",
                       "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", " ": " "})
_TOKEN = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")


def norm_tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens; decimal commas become points so 1,2 and 1.2 compare equal.
    Punctuation is ignored: '295ns' -> ['295', 'ns'], 'max(10 ns, 4 tCK)' -> ['max', '10', 'ns', '4', 'tck']."""
    out = []
    for t in _TOKEN.findall(text.translate(_FOLD).lower()):
        out.append(t.replace(",", ".") if re.fullmatch(r"\d+,\d+", t) else t)
    return out


@dataclass
class Box:
    x0: float
    y0: float
    x1: float
    y1: float
    words: tuple[int, ...] = ()

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 0.5)

    def rect(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]


def union(boxes: Iterable[Box]) -> Box:
    bs = list(boxes)
    return Box(min(b.x0 for b in bs), min(b.y0 for b in bs), max(b.x1 for b in bs), max(b.y1 for b in bs),
               tuple(sorted({w for b in bs for w in b.words})))


def v_overlap(a: Box, b: Box) -> float:
    """Vertical overlap relative to the smaller box height (1.0 = fully inside)."""
    return max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0)) / min(a.h, b.h)


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    def box(self, i: int) -> Box:
        return Box(self.x0, self.y0, self.x1, self.y1, (i,))


@dataclass
class TextMatch:
    status: str                 # exact | approximate | ambiguous | not_found | no_text_layer
    words: tuple[int, ...] = ()
    score: float = 0.0
    alternatives: int = 0


@dataclass
class PageGeom:
    page: int                   # 1-based physical page index
    label: str = ""             # printed page label (PDF page labels), "" if the PDF defines none
    width: float = 0.0          # rendered (rotated CropBox) size in points
    height: float = 0.0
    matrix: tuple = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)   # word space -> rendered space
    cropbox: tuple = (0.0, 0.0, 0.0, 0.0)            # unrotated, top-left origin
    mediabox_h: float = 0.0
    rotation: int = 0
    words: list[Word] = field(default_factory=list)
    _tokens: Optional[tuple[list[str], list[int]]] = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------ build/encode
    @classmethod
    def from_page(cls, page, page_no: int) -> "PageGeom":
        words = []
        for w in page.get_text("words"):
            if str(w[4]).strip():
                words.append(Word(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])))
        try:
            label = page.get_label() or ""
        except Exception:
            label = ""
        m = page.rotation_matrix
        cb, mb = page.cropbox, page.mediabox
        return cls(page_no, label, float(page.rect.width), float(page.rect.height),
                   (m.a, m.b, m.c, m.d, m.e, m.f), (cb.x0, cb.y0, cb.x1, cb.y1), float(mb.height),
                   int(page.rotation or 0), words)

    def encode(self) -> tuple[str, bytes]:
        meta = {"w": round(self.width, 2), "h": round(self.height, 2), "m": [round(v, 4) for v in self.matrix],
                "cb": [round(v, 2) for v in self.cropbox], "mbh": round(self.mediabox_h, 2), "rot": self.rotation}
        rows = [[round(w.x0, 1), round(w.y0, 1), round(w.x1, 1), round(w.y1, 1), w.text] for w in self.words]
        return json.dumps(meta), zlib.compress(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode(), 6)

    @classmethod
    def decode(cls, page: int, label: str, meta_json: str, blob: Optional[bytes]) -> "PageGeom":
        meta = json.loads(meta_json or "{}")
        rows = json.loads(zlib.decompress(blob).decode()) if blob else []
        return cls(page, label or "", meta.get("w", 0.0), meta.get("h", 0.0), tuple(meta.get("m", (1, 0, 0, 1, 0, 0))),
                   tuple(meta.get("cb", (0, 0, 0, 0))), meta.get("mbh", 0.0), meta.get("rot", 0),
                   [Word(r[0], r[1], r[2], r[3], r[4]) for r in rows])

    # ------------------------------------------------------------ transforms
    @property
    def has_text(self) -> bool:
        return len(self.words) >= 3

    def to_display(self, r: Sequence[float]) -> list[float]:
        """Word-space rect -> rendered page, normalised 0..1 (x0, y0, x1, y1)."""
        a, b, c, d, e, f = self.matrix
        xs, ys = [], []
        for x, y in ((r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3])):
            xs.append(a * x + c * y + e)
            ys.append(b * x + d * y + f)
        w, h = self.width or 1.0, self.height or 1.0
        return [round(max(0.0, min(xs) / w), 5), round(max(0.0, min(ys) / h), 5),
                round(min(1.0, max(xs) / w), 5), round(min(1.0, max(ys) / h), 5)]

    def to_pdf(self, r: Sequence[float]) -> list[float]:
        """Word-space rect -> PDF user space (points, origin bottom-left, unrotated)."""
        cx0, cy0 = self.cropbox[0], self.cropbox[1]
        return [round(r[0] + cx0, 2), round(self.mediabox_h - (r[3] + cy0), 2),
                round(r[2] + cx0, 2), round(self.mediabox_h - (r[1] + cy0), 2)]

    # ------------------------------------------------------------ tokens
    def tokens(self) -> tuple[list[str], list[int]]:
        if self._tokens is None:
            toks, owner = [], []
            for i, w in enumerate(self.words):
                for t in norm_tokens(w.text):
                    toks.append(t)
                    owner.append(i)
            self._tokens = (toks, owner)
        return self._tokens

    def box(self, word_ids: Iterable[int]) -> Box:
        return union(self.words[i].box(i) for i in word_ids)

    def line_boxes(self, word_ids: Iterable[int]) -> list[Box]:
        """One box per visual line of the given words (a multi-line sentence becomes several rects)."""
        lines: list[list[int]] = []
        for i in sorted(word_ids, key=lambda k: (self.words[k].y0, self.words[k].x0)):
            w = self.words[i].box(i)
            for ln in lines:
                if v_overlap(self.box(ln), w) > 0.5:
                    ln.append(i)
                    break
            else:
                lines.append([i])
        return [self.box(ln) for ln in lines]

    def find_phrase(self, text: str) -> list[Box]:
        """Exact token-sequence matches of text (each match as one box, in reading order)."""
        want = norm_tokens(text)
        if not want:
            return []
        toks, owner = self.tokens()
        out = []
        n = len(want)
        for i in range(len(toks) - n + 1):
            if toks[i] == want[0] and toks[i:i + n] == want:
                out.append(self.box(set(owner[i:i + n])))
        return out

    def locate_text(self, text: str, min_cov: float = 0.8) -> TextMatch:
        """Best location of a sentence/row of the chunk text on this page (tolerates cleanup differences:
        de-hyphenation, ligatures, removed headers). Ambiguous when two distinct places match equally."""
        if not self.has_text:
            return TextMatch("no_text_layer")
        want = norm_tokens(text)
        if not want:
            return TextMatch("not_found")
        toks, owner = self.tokens()
        if len(want) <= 2:
            hits = self.find_phrase(text)
            if len(hits) == 1:
                return TextMatch("exact", hits[0].words, 1.0)
            return TextMatch("ambiguous" if hits else "not_found", alternatives=len(hits))
        index: dict[str, list[int]] = {}
        for i, t in enumerate(toks):
            index.setdefault(t, []).append(i)
        rare = sorted(range(len(want)), key=lambda k: len(index.get(want[k], ())) or 10 ** 6)[:6]
        starts = set()
        for k in rare:
            for pos in index.get(want[k], [])[:200]:
                starts.add(pos - k)
        scored: list[tuple[float, int, int, tuple[int, ...]]] = []
        n = len(want)
        for s in sorted(starts):
            lo, hi = max(0, s - 3), min(len(toks), s + n + 3)
            sm = SequenceMatcher(None, want, toks[lo:hi], autojunk=False)
            blocks = [b for b in sm.get_matching_blocks() if b.size]
            matched = sum(b.size for b in blocks)
            if not blocks:
                continue
            a0 = lo + blocks[0].b
            a1 = lo + blocks[-1].b + blocks[-1].size
            scored.append((matched / n, a0, a1, tuple(sorted(set(owner[a0:a1])))))
        if not scored:
            return TextMatch("not_found")
        scored.sort(key=lambda x: -x[0])
        best = scored[0]
        if best[0] < min_cov:
            return TextMatch("not_found", score=best[0])
        rivals = [s for s in scored[1:] if s[0] >= best[0] - 0.05 and (s[2] <= best[1] or s[1] >= best[2])]
        if rivals:
            return TextMatch("ambiguous", score=best[0], alternatives=len(rivals) + 1)
        return TextMatch("exact" if best[0] >= 0.999 else "approximate", best[3], best[0])

    # ------------------------------------------------------------ table relations
    def visual_row(self, box: Box, tol: float = 0.5) -> list[int]:
        """Words on the same visual line as box."""
        return [i for i, w in enumerate(self.words) if v_overlap(w.box(i), box) > tol]


HEADER_WORDS = {"min", "minimum", "max", "maximum", "typ", "typical", "nom", "nominal", "unit", "units", "value",
                "values", "symbol", "parameter", "parameters", "description", "notes", "note", "conditions",
                "condition", "limits", "limit"}
FIELD_HEADERS = {
    "min": ["min", "minimum"],
    "max": ["max", "maximum"],
    "typ": ["typ", "typical", "nom", "nominal", "value", "values"],
}


@dataclass
class CellGrounding:
    status: str                         # grounded | ambiguous | conflict | not_found | no_text_layer
    value: Optional[Box] = None
    label: Optional[Box] = None
    header: Optional[Box] = None
    groups: list[Box] = field(default_factory=list)
    reason: str = ""
    page: int = 0
    header_page: int = 0


def _left_of(a: Box, b: Box, tol: float = 2.0) -> bool:
    return a.x1 <= b.x0 + tol


def _nearest_x(geom: PageGeom, row: Sequence[int], x: float) -> int:
    return min(row, key=lambda i: abs((geom.words[i].x0 + geom.words[i].x1) / 2 - x))


def _rows_with(geom: PageGeom, tokens: set[str]) -> list[list[int]]:
    """Visual rows (word ids) containing any of the tokens, bottom-most first."""
    toks, owner = geom.tokens()
    seeds = sorted({owner[i] for i, t in enumerate(toks) if t in tokens}, key=lambda i: -geom.words[i].y1)
    rows: list[list[int]] = []
    seen: set[int] = set()
    for i in seeds:
        if i in seen:
            continue
        row = geom.visual_row(geom.words[i].box(i))
        seen.update(row)
        rows.append(row)
    return rows


def _column_header(geom: PageGeom, c: Box, wanted: list[Box], head_toks: set[str],
                   x_only: bool = False) -> tuple[str, Optional[Box], str]:
    """Walk the header-like rows above c (nearest first). In each, the word nearest to c horizontally decides:
    part of a wanted header -> ('ok', box); another header keyword -> ('conflict', None, word); data -> go up."""
    wanted_ids = {w for b in wanted for w in b.words}
    for row in _rows_with(geom, HEADER_WORDS | head_toks):
        if not x_only and max(geom.words[i].y1 for i in row) > c.y0 + 1.0:
            continue  # not above the value
        near = _nearest_x(geom, row, c.cx)
        if near in wanted_ids:
            box = next(b for b in wanted if near in b.words)
            return "ok", box, geom.words[near].text
        if set(norm_tokens(geom.words[near].text)) & HEADER_WORDS:
            return "conflict", None, geom.words[near].text
    return "missing", None, ""


def ground_cell(geom: PageGeom, value: str, row_labels: Sequence[str], headers: Sequence[str],
                groups: Sequence[str] = (), prev: Optional[PageGeom] = None,
                label_left: bool = True) -> CellGrounding:
    """Find the one place on the page where `value` sits in the row of a row label and the column of a
    header (and, for multi-level headers, under the group label). Duplicated numbers elsewhere on the page do
    not count; two equally valid places make it ambiguous; a value found in its row but under a different
    column header is a conflict (e.g. min and max swapped).

    prev: the previous page, whose header row is used for a table continued from there.
    """
    if not geom.has_text:
        return CellGrounding("no_text_layer", page=geom.page)
    cands = geom.find_phrase(value)
    if not cands:
        return CellGrounding("not_found", reason=f"'{value}' is not in the text layer", page=geom.page)
    labels = [b for lab in row_labels if lab for b in geom.find_phrase(lab)]
    head_toks = {t for h in headers for t in norm_tokens(h)}
    wanted = [b for h in headers for b in geom.find_phrase(h)]
    prev_wanted = [b for h in headers for b in prev.find_phrase(h)] if prev is not None and prev.has_text else []
    good: list[CellGrounding] = []
    conflicts: list[str] = []
    row_hits = 0
    for c in cands:
        lab = next((lb for lb in labels if v_overlap(lb, c) > 0.3 and (not label_left or _left_of(lb, c))
                    and not set(lb.words) & set(c.words)), None)
        if lab is None:
            continue
        row_hits += 1
        state, header, word = _column_header(geom, c, wanted, head_toks)
        header_page = geom.page
        if state == "missing" and prev_wanted:
            state, header, word = _column_header(prev, c, prev_wanted, head_toks, x_only=True)
            header_page = prev.page
        if state == "conflict":
            conflicts.append(word)
            continue
        if state != "ok":
            continue
        group_boxes, group_ok = [], True
        for g in groups if header_page == geom.page else ():
            # A column group sits directly above the header row; elsewhere it is a row condition or context.
            gb = [b for b in geom.find_phrase(g)
                  if header.y0 - 3.5 * header.h <= b.y1 <= header.y0 + 1.0 and not v_overlap(b, header) > 0.5]
            if not gb:
                continue
            g_box = max(gb, key=lambda b: b.y1)
            gnear = _nearest_x(geom, geom.visual_row(g_box), c.cx)
            if gnear not in g_box.words:
                group_ok = False
                conflicts.append(f"group '{g}'")
                break
            group_boxes.append(g_box)
        if not group_ok:
            continue
        # A condition printed in the table body (a row condition such as '8 Gb') must be in the value's row.
        row_ok = True
        top = header.y1 - 1.0 if header_page == geom.page else 0.0
        for g in groups:
            body = [b for b in geom.find_phrase(g) if b.y0 >= top and abs(b.cy - c.cy) < 300 and b not in group_boxes]
            if body and not any(v_overlap(b, c) > 0.3 for b in body):
                row_ok = False
                conflicts.append(f"row of '{g}'")
                break
        if not row_ok:
            continue
        g = CellGrounding("grounded", c, lab, header, group_boxes, page=geom.page)
        g.header_page = header_page
        good.append(g)
    if len(good) == 1:
        return good[0]
    if len(good) > 1:
        return CellGrounding("ambiguous", reason=f"'{value}' matches {len(good)} cells", page=geom.page)
    if conflicts:
        return CellGrounding("conflict", reason=f"'{value}' is in the row but under '{conflicts[0]}'",
                             page=geom.page)
    if not row_hits:
        return CellGrounding("not_found", reason=f"'{value}' not found in the row of {[x for x in row_labels if x][:2]}",
                             page=geom.page)
    return CellGrounding("not_found", reason=f"no column header {list(headers)[:2]} above '{value}'", page=geom.page)


def find_unit(geom: PageGeom, unit: str, value: Box, header: Optional[Box]) -> tuple[str, Optional[Box]]:
    """('ok', box) when the unit is printed in the value's row, inside the value cell or in its column
    header; ('mismatch', box) when the row's unit cell holds a different unit; ('not_found', None)."""
    from techrag.units import is_unit_token

    want = norm_tokens(unit)
    if not want:
        return "ok", None
    for b in geom.find_phrase(unit):
        if v_overlap(b, value) > 0.3 and b.x0 >= value.x0 - 2:
            return "ok", b
        if header is not None and v_overlap(b, header) > 0.3 and abs(b.cx - header.cx) < 60:
            return "ok", b
    row = geom.visual_row(value, 0.3)
    for i in row:
        w = geom.words[i]
        if w.x0 > value.x1 and is_unit_token(w.text) and norm_tokens(w.text) != want:
            return "mismatch", w.box(i)
    return "not_found", None


def page_geoms_text(geoms: dict[int, PageGeom]) -> dict[int, str]:
    return {p: " ".join(w.text for w in g.words) for p, g in geoms.items()}
