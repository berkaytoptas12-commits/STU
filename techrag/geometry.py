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
# A minus sign belongs to the number it stands in front of ("-40", "(-40"), not to "DDR4-3200" or "1-5".
_TOKEN = re.compile(r"(?:(?<![a-z0-9.,])-(?=\d))?[a-z0-9]+(?:[.,][0-9]+)*")
_NUM_UNIT = re.compile(r"(-?\d+(?:\.\d+)*)([a-z]+)")


def norm_tokens(text: str) -> list[str]:
    """Lower-case alphanumeric tokens; an unambiguous decimal comma becomes a point so 1,2 and 1.2 compare equal
    ("1,200" may mean 1200 and stays as written). A leading minus is kept: -40 is not 40.
    Punctuation is ignored: '295ns' -> ['295', 'ns'], 'max(10 ns, 4 tCK)' -> ['max', '10', 'ns', '4', 'tck']."""
    out = []
    for t in _TOKEN.findall(text.translate(_FOLD).lower()):
        m = re.fullmatch(r"(-?)(\d+),(\d+)", t)
        if m and (len(m.group(3)) != 3 or m.group(2).lstrip("0") == "" or len(m.group(2)) > 3):
            t = f"{m.group(1)}{m.group(2)}.{m.group(3)}"
        nu = _NUM_UNIT.fullmatch(t)
        out.extend([nu.group(1), nu.group(2)] if nu else [t])
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
    sup: bool = False           # a raised footnote mark ("295" + superscript "1"), never a value of its own

    def box(self, i: int) -> Box:
        return Box(self.x0, self.y0, self.x1, self.y1, (i,))


# ------------------------------------------------------------------------------ superscripts

@dataclass
class SupMark:
    """A raised mark printed right after a word: a footnote mark (295 + superscript 1) or a power of ten
    (10 + superscript -12). The PDF text layer runs it into the word ("2951", "10-12")."""
    bbox: tuple                 # the mark (word space)
    base: str                   # the characters it is glued to ("295")
    text: str                   # the mark ("1")
    kind: str                   # note | power
    raw: str = ""               # base and mark as the text layer gives them ("2951", "10 -12")

    @property
    def rendered(self) -> str:
        """How the text shows it: 295⁽1⁾ (a footnote mark set apart), 10^-12 (a power of ten)."""
        return f"{self.base}^{self.text}" if self.kind == "power" else f"{self.base}⁽{self.text}⁾"


@dataclass
class SupLine:
    bbox: tuple
    raw: str                    # the line as the text layer gives it
    rendered: str               # the same line with its marks set apart
    marks: list[SupMark] = field(default_factory=list)


_MARK_TEXT = re.compile(r"[0-9a-zA-Z*†‡,]{1,4}")
_POWER = re.compile(r"[-−+]?\d{1,3}")
_UNIT_BASE = re.compile(r"(?:[munpkMGTµ]?(?:m|s|Hz|V|A|W|F|H|Ω|ohm|g))")
_NOT_MARKS = {"st", "nd", "rd", "th", "TM", "SM"}


def superscript_lines(page) -> list[SupLine]:
    """Lines of a PyMuPDF page with raised marks glued to the end of a word. A span is a mark when MuPDF flags it
    as superscript, or when it is clearly smaller and raised above the line's baseline. A small space the text
    layer puts between the word and the mark does not separate them. Not a mark: a raised character inside a
    word (I²C), an ordinal (1st), TM, a unit exponent (mm²) or a mark that stands alone."""
    out: list[SupLine] = []
    try:
        data = page.get_text("rawdict")
    except Exception:
        return out
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            spans = [sp for sp in line.get("spans", []) if sp.get("chars")]
            if len(spans) < 2:
                continue
            main = max(sp["size"] for sp in spans)
            base_y = next(sp["origin"][1] for sp in spans if sp["size"] == main)
            chars: list[tuple[str, float]] = []      # (char, size of its span)
            ranges: list[tuple[int, int]] = []
            for sp in spans:
                ranges.append((len(chars), len(chars) + len(sp["chars"])))
                chars += [(c["c"], sp["size"]) for c in sp["chars"]]
            raw = "".join(c for c, _ in chars)
            cuts: list[tuple[int, int, SupMark]] = []
            for sp, (start, end) in zip(spans, ranges):
                text = raw[start:end]
                raised = (sp.get("flags", 0) & 1) or (sp["size"] <= 0.8 * main and sp["origin"][1] < base_y - 0.2 * main)
                mark = text.strip()
                if not raised or sp["size"] >= 0.95 * main or not mark or mark != text:
                    continue
                k = start
                while k > 0 and raw[k - 1].isspace() and chars[k - 1][1] <= 0.8 * main and start - k < 2:
                    k -= 1          # a small filler space between the word and its mark
                b0 = k
                while b0 > 0 and not raw[b0 - 1].isspace():
                    b0 -= 1
                base = raw[b0:k]
                after = raw[end:end + 1]
                if not base or (after and not after.isspace() and after not in ",.;:)"):
                    continue          # stands alone, or sits inside a word (I²C)
                if _POWER.fullmatch(mark) and base.endswith("10"):
                    kind = "power"
                elif mark in _NOT_MARKS or (_UNIT_BASE.fullmatch(base) and mark in ("2", "3")):
                    continue
                elif _MARK_TEXT.fullmatch(mark):
                    kind = "note"
                else:
                    continue
                cuts.append((b0, end, SupMark(tuple(sp["bbox"]), base, mark, kind, raw[b0:end])))
            if not cuts:
                continue
            rendered, last = [], 0
            for b0, end, m in cuts:
                rendered.append(raw[last:b0] + m.rendered)
                last = end
            rendered.append(raw[last:])
            out.append(SupLine(tuple(line["bbox"]), raw, "".join(rendered), [m for _, _, m in cuts]))
    return out


def _split_marks(words: list[Word], lines: list[SupLine]) -> list[Word]:
    """Footnote marks become words of their own (sup=True): '2951' -> '295' + mark '1'. Powers of ten stay as
    the text layer gives them."""
    marks = [m for ln in lines for m in ln.marks if m.kind == "note"]
    if not marks:
        return words
    out: list[Word] = []
    for w in words:
        hit = next((m for m in marks
                    if m.bbox[0] >= w.x0 - 0.6 and m.bbox[2] <= w.x1 + 0.6
                    and min(m.bbox[3], w.y1) > max(m.bbox[1], w.y0)), None)
        if hit is None:
            out.append(w)
        elif w.text == hit.text:
            out.append(Word(w.x0, w.y0, w.x1, w.y1, w.text, True))
        elif w.text.endswith(hit.text) and w.text[:-len(hit.text)].endswith(hit.base[-1:]) \
                and len(w.text) > len(hit.text):
            cut = max(w.x0, min(hit.bbox[0], w.x1))
            out.append(Word(w.x0, w.y0, cut, w.y1, w.text[:-len(hit.text)]))
            out.append(Word(cut, hit.bbox[1], w.x1, hit.bbox[3], hit.text, True))
        else:
            out.append(w)
    return out


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
    _streams: Optional[list] = field(default=None, repr=False, compare=False)
    _matches: dict = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------ build/encode
    @classmethod
    def from_page(cls, page, page_no: int, sup_lines: Optional[list[SupLine]] = None) -> "PageGeom":
        words = []
        for w in page.get_text("words"):
            if str(w[4]).strip():
                words.append(Word(float(w[0]), float(w[1]), float(w[2]), float(w[3]), str(w[4])))
        words = _split_marks(words, superscript_lines(page) if sup_lines is None else sup_lines)
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
        rows = [[round(w.x0, 1), round(w.y0, 1), round(w.x1, 1), round(w.y1, 1), w.text] + ([1] if w.sup else [])
                for w in self.words]
        return json.dumps(meta), zlib.compress(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode(), 6)

    @classmethod
    def decode(cls, page: int, label: str, meta_json: str, blob: Optional[bytes]) -> "PageGeom":
        meta = json.loads(meta_json or "{}")
        rows = json.loads(zlib.decompress(blob).decode()) if blob else []
        return cls(page, label or "", meta.get("w", 0.0), meta.get("h", 0.0), tuple(meta.get("m", (1, 0, 0, 1, 0, 0))),
                   tuple(meta.get("cb", (0, 0, 0, 0))), meta.get("mbh", 0.0), meta.get("rot", 0),
                   [Word(r[0], r[1], r[2], r[3], r[4], bool(r[5]) if len(r) > 5 else False) for r in rows])

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
        """The page's tokens in reading order with the word each comes from (footnote marks left out)."""
        if self._tokens is None:
            toks, owner = [], []
            for i, w in enumerate(self.words):
                if w.sup:
                    continue
                for t in norm_tokens(w.text):
                    toks.append(t)
                    owner.append(i)
            self._tokens = (toks, owner)
        return self._tokens

    def _glued(self, a: Word, b: Word) -> bool:
        """b is printed directly after a with no space: a subscript (t + RFC) or a kerning split. Not a raised
        footnote mark."""
        h = max(a.y1 - a.y0, 1.0)
        return (not a.sup and not b.sup and -0.5 <= b.x0 - a.x1 <= max(0.8, 0.12 * h)
                and min(a.y1, b.y1) - max(a.y0, b.y0) > 0.3 * min(h, max(b.y1 - b.y0, 1.0)))

    def streams(self) -> list[tuple[list[str], list[tuple[int, ...]]]]:
        """Token streams to match phrases against, each token with the words it comes from:
        1. as printed (footnote marks left out),
        2. glued words joined: a symbol set as t + subscript RFC reads 'trfc',
        3. footnote marks kept as tokens of their own: '295 (1)' finds 295 with its mark."""
        if self._streams is None:
            toks, owner = self.tokens()
            plain = (toks, [(o,) for o in owner])
            jt: list[str] = []
            jo: list[tuple[int, ...]] = []
            prev = None
            for i, w in enumerate(self.words):
                if w.sup:
                    continue
                ts = norm_tokens(w.text)
                if not ts:
                    continue
                if prev is not None and jt and self._glued(self.words[prev], w):
                    jt[-1] += ts[0]
                    jo[-1] = jo[-1] + (i,)
                    ts = ts[1:]
                for t in ts:
                    jt.append(t)
                    jo.append((i,))
                prev = i
            ft: list[str] = []
            fo: list[tuple[int, ...]] = []
            for i, w in enumerate(self.words):
                for t in norm_tokens(w.text):
                    ft.append(t)
                    fo.append((i,))
            self._streams = [plain, (jt, jo), (ft, fo)]
        return self._streams

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

    def _match_tokens(self, want: list[str]) -> list[Box]:
        key = tuple(want)
        if key not in self._matches:
            self._matches[key] = self._scan(want)
        return self._matches[key]

    def _scan(self, want: list[str]) -> list[Box]:
        seen: dict[frozenset, Box] = {}
        n = len(want)
        for toks, owner in self.streams():
            for i in range(len(toks) - n + 1):
                if toks[i] == want[0] and toks[i:i + n] == want:
                    ids = frozenset(w for o in owner[i:i + n] for w in o)
                    if ids not in seen and not all(self.words[w].sup for w in ids):
                        seen[ids] = self.box(ids)
        return sorted(seen.values(), key=lambda b: min(b.words))

    def find_phrase(self, text: str, label: bool = False) -> list[Box]:
        """Exact token-sequence matches of text (each match as one box, in reading order). Glued words count as
        one (t + subscript RFC = tRFC); a footnote mark is skipped ('295' finds 295 with a raised 1 after it)
        but may also be named ('295 (1)'); a match is never a footnote mark alone.

        label=True (row labels): a symbol written with a space ('t RFC', 'V DDQ') also finds the symbol as
        printed in one piece."""
        want = norm_tokens(text)
        if not want:
            return []
        out = self._match_tokens(want)
        if label and not out and len(want) == 2 and len(want[0]) == 1 and want[0].isalpha():
            out = self._match_tokens([want[0] + want[1]])
        return out

    def find_wrapped(self, text: str, max_lines: int = 3) -> list[Box]:
        """A row label printed over several lines of its cell ('Refresh cycle time for' / '8Gb devices'): the
        first part on one line, each next part on the line directly below, starting at the same x (or centred
        on the same axis). Returns one box per full match."""
        want = norm_tokens(text)
        if len(want) < 2:
            return []
        out: dict[frozenset, Box] = {}
        for k in range(len(want) - 1, 0, -1):
            for first in self._match_tokens(want[:k]):
                for rest in self._below(want[k:], first, max_lines - 1):
                    box = union([first, rest])
                    out.setdefault(frozenset(box.words), box)
        return sorted(out.values(), key=lambda b: min(b.words))

    def _below(self, want: list[str], above: Box, lines: int) -> list[Box]:
        if lines <= 0 or not want:
            return []

        def under(b: Box) -> bool:
            return (above.y0 + 0.5 * above_h <= b.y0 <= above_y1 + 1.0 * above_h
                    and (abs(b.x0 - above.x0) <= 3.0 or abs(b.cx - above.cx) <= 3.0))

        above_h = max(min(self.words[i].y1 - self.words[i].y0 for i in above.words), 1.0)
        above_y1 = max(self.words[i].y1 for i in above.words)
        found = [b for b in self._match_tokens(want) if under(b)]
        if found or lines == 1:
            return found
        out = []
        for k in range(len(want) - 1, 0, -1):
            for mid in (b for b in self._match_tokens(want[:k]) if under(b)):
                out += [union([mid, r]) for r in self._below(want[k:], mid, lines - 1)]
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
    labels = []
    for lab in row_labels:
        if lab:
            labels += geom.find_phrase(lab, label=True) or geom.find_wrapped(lab)
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
