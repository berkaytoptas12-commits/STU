"""Document metadata and relations.

Identity is kept in separate fields, because "same standard tag" does not mean "same document":
* entities  - standard families/versions the document is about (DDR5, PCIe 5.0) - used for scoping;
* doc_key   - the document series (JESD79-5, "pci express base", "pci express cem", "ddr4 layout guide");
* version   - the standard version the document belongs to (5.0), when stated;
* revision  - the revision of that document (C, 1.0);
* doc_type  - base | errata | ecn | amendment | guide | appnote | other.

Only revisions of the same series, version and type supersede each other (a CEM spec never supersedes a
Base spec; a design guide never overrides anything). Errata/ECN are linked to the base document of the
same series and version they amend (techrag.retrieval.DocCatalog), not to everything with the same tag.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from techrag.domains import DomainRegistry
from techrag.llm import LLMClient, parse_json_object

META_PROMPT_VERSION = "meta-v1"

_DOC_TYPES = [
    ("errata", r"\berrat(?:a|um)\b|\bcorrigend(?:a|um)\b"),
    ("ecn", r"\b(?:ecn|ecr|engineering change (?:notice|request))\b"),
    ("amendment", r"\bamendment\b|\baddendum\b"),
    ("appnote", r"\bapplication note\b|\bapp[\s_-]?note\b"),
    ("guide", r"\b(?:user'?s?|design|layout) guide(?:lines?)?\b"),
]
_REVISION = re.compile(r"\b(?:rev(?:ision)?|version|ver)\.?\s*([0-9]+(?:\.[0-9]+)*[a-z]?|[a-z])\b|"
                       r"(?<![a-z0-9])r(\d+(?:\.\d+)+)\b", re.I)
_JESD = re.compile(r"\bjesd\d+(?:-\d+)?([a-z])\b", re.I)
_DATE = re.compile(r"\b((?:19|20)\d{2})(?:[-/.](0[1-9]|1[0-2]))?\b")
_MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                            "august", "september", "october", "november", "december"])}
_MONTH_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+((?:19|20)\d{2})\b", re.I)

SYSTEM = """You read the first pages of a technical standard document and return ONLY a JSON object:
{"title": "", "standard": "", "version": "", "revision": "", "date": "YYYY-MM or YYYY or empty",
 "doc_type": "base|errata|ecn|amendment|appnote|guide|other"}
- "standard": the standard family and number as named in the document (e.g. "PCI Express Base Specification", "JESD79-5 DDR5 SDRAM", "ARINC 429 Part 1", "USB Power Delivery").
- "version"/"revision": exactly as written (e.g. "5.0", "1.0", "B", "3.1").
- "doc_type": base = the specification itself; errata/ecn/amendment = changes to it.
Use "" when the pages do not say. Do not guess."""


@dataclass
class DocMeta:
    title: str = ""
    entities: list[str] = field(default_factory=list)
    doc_type: str = "base"
    revision: str = ""
    doc_date: str = ""
    llm: dict = field(default_factory=dict)
    doc_key: str = ""
    version: str = ""
    part: str = ""


AMENDING_TYPES = ("errata", "ecn", "amendment")
INFORMATIVE_TYPES = ("guide", "appnote", "other")

_ALIASES = [(r"\bpci[\s-]?e(?:xpress)?\b", "pci express"), (r"\bspec(?:ification)?s?\b", " "),
            (r"\bstandards?\b", " "), (r"\bjedec\b", " "), (r"\bdocument\b", " "), (r"\bthe\b", " ")]
_SERIES_NOISE = re.compile(
    r"\b(?:errat(?:a|um)|corrigend(?:a|um)|ecn|ecr|engineering|change|notice|request|amendment|addendum|draft|"
    r"final|clean|redline|copy|rev(?:ision)?|version|ver|r\d+(?:\.\d+)*|v\d+(?:\.\d+)*|\d+(?:\.\d+)+[a-z]?|"
    r"(?:19|20)\d{2}|[a-z])\b")
_JESD_ID = re.compile(r"\bjesd\s?(\d+)(?:-(\d+))?", re.I)
_VERSION = re.compile(r"(?<![\w.])(?<!rev )(?<!r)(\d+\.\d+)(?![\w.])", re.I)


def series_key(*texts: str) -> str:
    """Document series identity without revision/version/date/type words: 'JESD79-4C' -> 'jesd79-4',
    'PCIe_Base_5.0_r1.0' and 'PCIe Base 5.0 Errata' -> 'pci express base', 'PCIe CEM 5.0' -> 'pci express cem'."""
    text = " ".join(t for t in texts if t).replace("_", " ").lower()
    m = _JESD_ID.search(text)
    if m:
        return f"jesd{m.group(1)}" + (f"-{m.group(2)}" if m.group(2) else "")
    for pat, rep in _ALIASES:
        text = re.sub(pat, rep, text)
    text = re.sub(r"\b(?:rev(?:ision)?|version|ver)\.?\s*[a-z0-9]+(?:\.[0-9]+)*\b", " ", text)
    text = _SERIES_NOISE.sub(" ", text)
    return " ".join(re.findall(r"[a-z0-9.]+", text))


def regex_version(text: str) -> str:
    """Standard version printed in a file name/title ('PCIe_Base_5.0_r1.0' -> '5.0'); '' when absent."""
    m = _VERSION.search((text or "").replace("_", " "))
    return m.group(1) if m else ""


def part_of(doc_type: str, text: str) -> str:
    t = (text or "").lower()
    for name, pat in (("cem", r"\bcem\b|card electromechanical"), ("m.2", r"\bm\.2\b"),
                      ("part", r"\bpart\s*\d+")):
        m = re.search(pat, t)
        if m:
            return m.group(0) if name == "part" else name
    return doc_type if doc_type != "base" else "base"


def regex_doc_type(text: str) -> Optional[str]:
    for name, pat in _DOC_TYPES:
        if re.search(pat, text, re.I):
            return name
    return None


def regex_revision(text: str) -> str:
    m = _JESD.search(text)
    if m:
        return m.group(1).upper()
    m = _REVISION.search(text)
    return (m.group(1) or m.group(2)) if m else ""


def regex_date(text: str) -> str:
    m = _MONTH_DATE.search(text)
    if m:
        return f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"
    m = _DATE.search(text)
    if m:
        return m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")
    return ""


def llm_metadata(client: LLMClient, front_text: str, doc_sha: str, cache_dir: Path,
                 read_only_cache: bool = False) -> dict:
    key = hashlib.sha256(f"{META_PROMPT_VERSION}|{client.model}".encode()).hexdigest()[:12]
    cache = Path(cache_dir) / doc_sha[:24] / f"meta_{key}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    res = client.chat([{"role": "system", "content": SYSTEM},
                       {"role": "user", "content": front_text[:6000]}],
                      json_mode=True, max_tokens=400, temperature=0.0, thinking=False)
    obj = parse_json_object(res.content) or {}
    obj = {k: str(obj.get(k, "") or "") for k in ("title", "standard", "version", "revision", "date", "doc_type")}
    if not read_only_cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return obj


def build_metadata(registry: DomainRegistry, domain: str, filename: str, pdf_title: str, front_text: str,
                   llm: Optional[dict] = None) -> DocMeta:
    llm = llm or {}
    extra = [llm.get("standard", ""), f"{llm.get('standard', '')} {llm.get('version', '')}", llm.get("title", "")]
    entities = registry.document_entities(domain, filename, pdf_title, front_text[:4000],
                                          [e for e in extra if e.strip()])
    head = f"{filename} {pdf_title} {llm.get('title', '')}"
    doc_type = regex_doc_type(head) or (llm.get("doc_type") or "").lower() or regex_doc_type(front_text[:1500]) or "base"
    if doc_type not in ("base", "errata", "ecn", "amendment", "appnote", "guide", "other"):
        doc_type = "other"
    revision = llm.get("revision") or llm.get("version") or regex_revision(f"{filename} {front_text[:3000]}")
    date = llm.get("date") or regex_date(front_text[:4000])
    version = llm.get("version") if llm.get("revision") else ""
    version = version or regex_version(f"{filename} {pdf_title}")
    key = series_key(filename, pdf_title) or series_key(llm.get("standard", ""))
    return DocMeta(title=llm.get("title", ""), entities=entities, doc_type=doc_type, revision=revision,
                   doc_date=date, llm=llm, doc_key=key, version=version,
                   part=part_of(doc_type, f"{filename} {pdf_title} {llm.get('title', '')}"))


def _rev_key(rev: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.findall(r"\d+|[a-zA-Z]+", rev or ""))


def doc_series(d) -> str:
    return getattr(d, "doc_key", "") or series_key(getattr(d, "path", "").rsplit("/", 1)[-1].rsplit(".", 1)[0],
                                                    getattr(d, "title", ""))


def compute_supersedence(docs) -> list[tuple[int, Optional[int]]]:
    """Older revisions of the SAME document (same collection, series, version, entity set, base type) are
    superseded by the newest. Different documents with the same standard tag (Base vs CEM, a spec vs a
    design guide, errata) never supersede each other. Decided only when dates or revisions are comparable;
    returns [(doc_id, newer_doc_id or None)]."""
    groups: dict[tuple, list] = {}
    for d in docs:
        key = doc_series(d)
        if d.doc_type == "base" and d.entities and key:
            groups.setdefault((d.domain, key, getattr(d, "version", ""), tuple(sorted(d.entities))), []).append(d)
    result: dict[int, Optional[int]] = {d.id: None for d in docs}
    for members in groups.values():
        if len(members) < 2:
            continue
        if all(m.doc_date for m in members):
            key = lambda m: (m.doc_date, _rev_key(m.revision))  # noqa: E731
        elif all(m.revision for m in members):
            key = lambda m: _rev_key(m.revision)  # noqa: E731
        else:
            continue
        ordered = sorted(members, key=key)
        newest = ordered[-1]
        if key(ordered[-2]) == key(newest):
            continue
        for m in ordered[:-1]:
            result[m.id] = newest.id
    return [(doc_id, newer) for doc_id, newer in result.items()]


def amended_targets(doc, docs) -> list[int]:
    """Base documents an errata/ECN/amendment applies to: same collection and series, same version when the
    amending document states one (otherwise only an unambiguous single candidate)."""
    if doc.doc_type not in AMENDING_TYPES:
        return []
    key = doc_series(doc)
    cands = [d for d in docs if d.id != doc.id and d.doc_type == "base" and d.domain == doc.domain
             and key and doc_series(d) == key]
    ver = getattr(doc, "version", "")
    if ver:
        cands = [d for d in cands if getattr(d, "version", "") in (ver, "")]
        active = [d for d in cands if d.superseded_by is None]
        return [d.id for d in (active or cands)]
    active = [d for d in cands if d.superseded_by is None]
    return [active[0].id] if len(active) == 1 else []
