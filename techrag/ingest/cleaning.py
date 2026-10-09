"""Text cleanup for extracted standard documents."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from techrag.ingest.loaders import TABLE_KIND, Block

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
              "ﬅ": "st", "ﬆ": "st"}
_SPACES = re.compile(r"[ \t  -   　]+")
_DOT_LEADER = re.compile(r"(?:\.\s?){4,}\s*[\divxlcIVXLC]+\s*$|\s{3,}\d+\s*$")


def normalize_text(text: str) -> str:
    for k, v in _LIGATURES.items():
        text = text.replace(k, v)
    text = text.replace("­", "")  # soft hyphen
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Re-join words hyphenated at a line break: "termi-\nnation" -> "termination".
    text = re.sub(r"([a-z]{2,})-\n([a-z]{2,})", r"\1\2", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _exact_key(line: str) -> str:
    return re.sub(r"\s+", " ", line.lower()).strip()


def _number_key(line: str) -> str:
    """Normalize a header/footer candidate so 'Page 12 of 300' and 'Page 13 of 300' collide."""
    key = re.sub(r"\d+", "#", line.lower())
    key = re.sub(r"\b[ivxlc]+\b", "#", key)  # roman page numbers
    return re.sub(r"\s+", " ", key).strip()


def remove_headers_footers(blocks: list[Block], n_pages: int, edge_lines: int = 3) -> list[Block]:
    """Drop running headers, footers and page numbers.

    * A line within the top/bottom ``edge_lines`` of a page is removed when the exact same line sits at a
      page edge on many pages ("PCI Express Base Specification, Rev. 5.0", "Chapter 4 Physical Layer").
    * The very first/last line of a page is also removed when it only differs by numbers across pages
      ("Page 12 of 300", "JESD79-4C Page 23") or is a bare page number.
    """
    if n_pages < 4:
        return blocks
    by_page: dict[int, list[int]] = defaultdict(list)
    for i, b in enumerate(blocks):
        if b.kind != TABLE_KIND:
            by_page[b.page].append(i)

    split = {i: blocks[i].text.split("\n") for idxs in by_page.values() for i in idxs}
    edges: dict[int, list[tuple[int, int, bool]]] = {}  # page -> [(block, line, is_first_or_last)]
    exact: Counter = Counter()
    numeric: Counter = Counter()
    for page, idxs in by_page.items():
        lines = [(bi, li) for bi in idxs for li in range(len(split[bi]))]
        if not lines:
            continue
        outer = {lines[0], lines[-1]}
        cand = list(dict.fromkeys(lines[:edge_lines] + lines[-edge_lines:]))
        edges[page] = [(bi, li, (bi, li) in outer) for bi, li in cand]
        exact.update({_exact_key(split[bi][li]) for bi, li in cand} - {""})
        numeric.update({_number_key(split[bi][li]) for bi, li in outer
                        if len(split[bi][li].split()) <= 10} - {""})

    # Running chapter headers only span part of a long standard, so the bar is deliberately low.
    threshold = max(3, int(0.08 * len(by_page)))
    drop: dict[int, set[int]] = defaultdict(set)
    for page, cand in edges.items():
        for bi, li, is_outer in cand:
            line = split[bi][li]
            if exact[_exact_key(line)] >= threshold:
                drop[bi].add(li)
            elif is_outer and (numeric[_number_key(line)] >= threshold or _number_key(line) == "#"):
                drop[bi].add(li)

    out = []
    for i, b in enumerate(blocks):
        if i in drop:
            kept = [ln for li, ln in enumerate(split[i]) if li not in drop[i]]
            text = "\n".join(kept).strip()
            if not text:
                continue
            b = Block(b.page, text, b.kind, b.ref)
        out.append(b)
    return out


def toc_like_pages(blocks: list[Block], min_ratio: float = 0.4) -> set[int]:
    """Pages that are a printed table of contents / list of figures (dot leaders + page numbers)."""
    lines_per_page: dict[int, list[str]] = defaultdict(list)
    for b in blocks:
        if b.kind != TABLE_KIND:
            lines_per_page[b.page].extend(ln for ln in b.text.split("\n") if ln.strip())
    pages = set()
    for page, lines in lines_per_page.items():
        if len(lines) < 5:
            continue
        hits = sum(1 for ln in lines if _DOT_LEADER.search(ln))
        if hits / len(lines) >= min_ratio:
            pages.add(page)
    return pages


def clean_blocks(blocks: list[Block], n_pages: int, strip_headers_footers: bool = True,
                 skip_toc_pages: bool = True) -> list[Block]:
    blocks = [Block(b.page, b.text if b.kind == TABLE_KIND else normalize_text(b.text), b.kind, b.ref)
              for b in blocks]
    blocks = [b for b in blocks if b.text.strip()]
    if strip_headers_footers:
        blocks = remove_headers_footers(blocks, n_pages)
    if skip_toc_pages:
        skip = toc_like_pages(blocks)
        blocks = [b for b in blocks if b.page not in skip]
    return blocks
