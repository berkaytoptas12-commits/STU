"""Section-aware chunking.

* Chunks never cross a section boundary, so every chunk has one precise section + page range.
* Tables become their own chunks (split by rows with the header repeated) and keep their caption.
* Consecutive chunks of one section overlap by a few sentences; the overlap length is recorded so
  neighbouring chunks can be stitched back together without duplicated text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from techrag.ingest.loaders import TABLE_KIND, TEXT_KIND
from techrag.ingest.structure import SectionedBlock

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+(?=[A-Z0-9(\"'•\-])|\n(?=[•\-*–]|\d+[.)]\s)")
_CAPTION = re.compile(r"^(Table|Tab\.|Tablo)\s*[A-Z]?[\d.\-–]+", re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    """Cheap, tokenizer-free estimate; technical text averages ~4 chars per token."""
    return max(len(text) // 4, int(len(text.split()) * 1.3))


@dataclass
class Chunk:
    ordinal: int
    text: str
    section: tuple[str, ...]
    page_start: int
    page_end: int
    kind: str = TEXT_KIND
    overlap: int = 0  # number of leading characters copied from the previous chunk

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def _split_long(text: str, max_tokens: int) -> list[str]:
    if estimate_tokens(text) <= max_tokens:
        return [text]
    pieces: list[str] = []
    buf = ""
    for sent in _SENTENCE_SPLIT.split(text):
        if not sent:
            continue
        if estimate_tokens(sent) > max_tokens:
            if buf:
                pieces.append(buf)
                buf = ""
            words = sent.split(" ")
            step = max(1, int(len(words) * max_tokens / estimate_tokens(sent)))
            pieces.extend(" ".join(words[i:i + step]) for i in range(0, len(words), step))
            continue
        candidate = f"{buf} {sent}".strip() if buf else sent
        if estimate_tokens(candidate) > max_tokens and buf:
            pieces.append(buf)
            buf = sent
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces


def _tail(text: str, overlap_tokens: int) -> str:
    """Last sentences of text totalling about overlap_tokens."""
    if overlap_tokens <= 0:
        return ""
    sents = [s for s in _SENTENCE_SPLIT.split(text) if s]
    out: list[str] = []
    for s in reversed(sents):
        if estimate_tokens(" ".join([s, *out])) > overlap_tokens:
            break
        out.insert(0, s)
    return " ".join(out)


def split_table(markdown: str, max_tokens: int) -> list[str]:
    lines = markdown.split("\n")
    if estimate_tokens(markdown) <= max_tokens or len(lines) <= 3:
        return [markdown]
    header, rows = lines[:2], lines[2:]
    parts, buf = [], []
    for row in rows:
        if buf and estimate_tokens("\n".join(header + buf + [row])) > max_tokens:
            parts.append("\n".join(header + buf))
            buf = []
        buf.append(row)
    if buf:
        parts.append("\n".join(header + buf))
    return parts


def chunk_blocks(blocks: list[SectionedBlock], target_tokens: int = 380, max_tokens: int = 650,
                 overlap_tokens: int = 60, min_tokens: int = 40) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[tuple[str, int]] = []  # (text, page)
    buf_overlap = 0
    section: tuple[str, ...] | None = None
    last_text_line = ""

    def emit(text: str, pages: list[int], kind: str, overlap: int = 0):
        chunks.append(Chunk(len(chunks), text, section or (), min(pages), max(pages), kind, overlap))

    def flush(carry_overlap: bool):
        nonlocal buf, buf_overlap
        if not buf:
            return
        text = "\n\n".join(t for t, _ in buf)
        emit(text, [p for _, p in buf], TEXT_KIND, buf_overlap)
        if carry_overlap:
            tail = _tail(buf[-1][0], overlap_tokens)
            buf = [(tail, buf[-1][1])] if tail and tail != buf[-1][0] else []
            buf_overlap = len(tail) + 2 if buf else 0
        else:
            buf, buf_overlap = [], 0

    for b in blocks:
        if b.section != section:
            flush(carry_overlap=False)
            section = b.section
        if b.kind == TABLE_KIND:
            flush(carry_overlap=False)
            caption = last_text_line if _CAPTION.match(last_text_line) else ""
            last_text_line = ""
            for part in split_table(b.text, max_tokens):
                emit(f"{caption}\n{part}" if caption else part, [b.page], TABLE_KIND)
            continue
        lines = [ln for ln in b.text.split("\n") if ln.strip()]
        last_text_line = lines[-1].strip() if lines else ""
        for piece in _split_long(b.text, max_tokens):
            current = sum(estimate_tokens(t) for t, _ in buf)
            if buf and current + estimate_tokens(piece) > target_tokens:
                flush(carry_overlap=True)
            buf.append((piece, b.page))
    flush(carry_overlap=False)
    return _merge_tiny(chunks, min_tokens, max_tokens)


def _merge_tiny(chunks: list[Chunk], min_tokens: int, max_tokens: int) -> list[Chunk]:
    """Fold very short text chunks into a neighbour of the same section.

    A heading-only chunk ("4.2.6 LTSSM") is merged forward into its first subsection; a short tail is
    merged back into its own section. Short chunks of a genuinely short section stay on their own so
    no text is ever attributed to the wrong section.
    """
    out: list[Chunk] = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        if c.kind != TEXT_KIND or c.tokens >= min_tokens:
            out.append(c)
            i += 1
            continue
        nxt = chunks[i + 1] if i + 1 < len(chunks) else None
        prev = out[-1] if out else None
        if (nxt is not None and nxt.kind == TEXT_KIND and nxt.section[:len(c.section)] == c.section
                and c.tokens + nxt.tokens <= max_tokens):
            chunks[i + 1] = Chunk(nxt.ordinal, f"{c.text}\n\n{nxt.text[nxt.overlap:]}", nxt.section,
                                  min(c.page_start, nxt.page_start), max(c.page_end, nxt.page_end), nxt.kind, 0)
        elif (prev is not None and prev.kind == TEXT_KIND and prev.section == c.section
              and prev.tokens + c.tokens <= max_tokens):
            out[-1] = Chunk(prev.ordinal, f"{prev.text}\n\n{c.text[c.overlap:]}", prev.section,
                            prev.page_start, max(prev.page_end, c.page_end), prev.kind, prev.overlap)
        else:
            out.append(c)
        i += 1
    for n, c in enumerate(out):
        c.ordinal = n
    return out
