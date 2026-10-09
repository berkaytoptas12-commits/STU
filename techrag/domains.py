"""Collections (one folder per interface / standard group), standard entities and query term expansion.

Entity resolution for standards: every document is tagged with canonical standard ids (e.g. "DDR5",
"PCIe 5.0", "ARINC 429"). When a question names a standard explicitly, retrieval is hard-filtered to
documents carrying that id, so a sibling standard (DDR4 vs DDR5) can never enter the context.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

_TR_FOLD = str.maketrans({
    "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "I": "i", "İ": "i",
    "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u", "â": "a", "î": "i", "û": "u",
})


def fold_tr(text: str) -> str:
    """Lowercase + fold Turkish letters to ASCII so 'Hızı', 'hizi', 'HIZI' compare equal."""
    return text.translate(_TR_FOLD).lower()


@dataclass
class Entity:
    id: str
    domain: str
    patterns: list[re.Pattern] = field(default_factory=list)

    def count(self, text: str) -> int:
        return sum(len(p.findall(text)) for p in self.patterns)


@dataclass
class Domain:
    key: str
    name: str
    folders: list[str] = field(default_factory=list)
    patterns: list[re.Pattern] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)
    entities: list[Entity] = field(default_factory=list)


class DomainRegistry:
    _SHORT_SUFFIXES = ("i", "u", "e", "a", "si", "su", "yi", "yu", "ye", "ya", "in", "un", "nin", "nun",
                       "de", "da", "te", "ta", "den", "dan", "ten", "tan", "ler", "lar", "leri", "lari")
    # Final-consonant softening before a vowel suffix: uzunluk -> uzunluğu, tip -> tibi ...
    _SOFTEN = {"k": "[kg]", "p": "[pb]", "t": "[td]"}

    def __init__(self, domains: dict[str, Domain], term_map: dict[str, str]):
        self.domains = domains
        self.term_map = term_map
        self._terms = [(self._term_pattern(fold_tr(k)), v) for k, v in term_map.items()]
        self._rebuild()

    def _rebuild(self) -> None:
        self._folder_index = {f.lower(): d.key for d in self.domains.values() for f in [d.key, *d.folders]}
        self.entities = {e.id: e for d in self.domains.values() for e in d.entities}
        self._acronyms: list[tuple[re.Pattern, str, str]] = []
        for d in self.domains.values():
            for acro, expansion in d.glossary.items():
                flags = 0 if len(acro) <= 3 else re.IGNORECASE
                pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(acro) + r"(?![A-Za-z0-9])", flags)
                self._acronyms.append((pat, acro, expansion))

    @classmethod
    def _term_pattern(cls, folded_key: str) -> re.Pattern:
        last = folded_key[-1]
        stem = re.escape(folded_key[:-1]) + cls._SOFTEN.get(last, re.escape(last))
        head = r"(?<![a-z0-9])" + stem
        if len(folded_key) <= 4:
            return re.compile(head + "(?:" + "|".join(cls._SHORT_SUFFIXES) + r")?(?![a-z0-9])")
        return re.compile(head)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path) -> "DomainRegistry":
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        domains = {}
        for key, spec in (raw.get("domains") or {}).items():
            spec = spec or {}
            domains[key] = Domain(
                key=key,
                name=spec.get("name", key),
                folders=[str(f) for f in spec.get("folders", [])],
                patterns=[re.compile(p, re.IGNORECASE) for p in spec.get("patterns", [])],
                glossary={str(k): str(v) for k, v in (spec.get("glossary") or {}).items()},
                entities=[Entity(str(e["id"]), key, [re.compile(p, re.IGNORECASE) for p in e.get("patterns", [])])
                          for e in spec.get("standards", []) or []],
            )
        if "general" not in domains:
            domains["general"] = Domain(key="general", name="General")
        term_map = {str(k): str(v) for k, v in (raw.get("term_map") or {}).items()}
        return cls(domains, term_map)

    def ensure_collection(self, folder: str) -> str:
        """A top-level source folder that is not configured becomes its own collection."""
        key = self.domain_for_folder(folder)
        if key:
            return key
        key = re.sub(r"[^a-z0-9_-]+", "_", folder.lower()).strip("_") or "general"
        if key not in self.domains:
            self.domains[key] = Domain(key=key, name=folder)
            self._rebuild()
        return key

    # ---------------------------------------------------------------- collections
    def keys(self) -> list[str]:
        return list(self.domains)

    def name(self, key: str) -> str:
        d = self.domains.get(key)
        return d.name if d else key

    def domain_for_folder(self, folder: str) -> Optional[str]:
        return self._folder_index.get(folder.lower())

    def score(self, text: str) -> dict[str, int]:
        scores: dict[str, int] = {}
        for d in self.domains.values():
            n = sum(len(p.findall(text)) for p in d.patterns) + sum(e.count(text) for e in d.entities)
            if n:
                scores[d.key] = n
        return scores

    def detect(self, text: str, exclude_general: bool = False) -> list[str]:
        """Collections explicitly referenced in a question, strongest first."""
        scores = self.score(text)
        if exclude_general:
            scores.pop("general", None)
        return [k for k, _ in sorted(scores.items(), key=lambda kv: -kv[1])]

    def classify_document(self, rel_path: Path, sample_text: str) -> str:
        """Collection of a source file: its top-level folder, else filename, else content keywords."""
        if len(rel_path.parts) > 1:
            return self.ensure_collection(rel_path.parts[0])
        name_hits = self.detect(rel_path.stem.replace("_", " "))
        if name_hits:
            return name_hits[0]
        content = self.score(sample_text)
        if content:
            best, n = max(content.items(), key=lambda kv: kv[1])
            if n >= 3:
                return best
        return "general"

    # ------------------------------------------------------------------- entities
    def detect_entities(self, text: str, domains: Optional[Iterable[str]] = None) -> list[str]:
        allowed = set(domains) if domains else None
        hits = [(e.id, e.count(text)) for e in self.entities.values() if allowed is None or e.domain in allowed]
        return [eid for eid, n in sorted(hits, key=lambda x: -x[1]) if n]

    def document_entities(self, domain: str, filename: str, title_text: str, front_text: str,
                          extra: Iterable[str] = ()) -> list[str]:
        """Standards a document *is about* (not merely mentions): filename/title first, then the front
        matter, where only the dominant entity counts. ``extra`` are strings from LLM metadata."""
        doms = [domain] if domain in self.domains and self.domains[domain].entities else None
        head = " ".join([filename.replace("_", " "), title_text, *extra])
        # The collection (a folder name) only narrows the candidates; it is never evidence by itself. When the
        # document's own name/title names a standard of another collection, that standard wins.
        strong = self.detect_entities(head, doms) or (self.detect_entities(head) if doms else [])
        if strong:
            return strong
        counts = Counter({eid: self.entities[eid].count(front_text) for eid in self.detect_entities(front_text, doms)})
        if not counts:
            return []
        top = max(counts.values())
        return [eid for eid, n in counts.items() if n >= max(2, top * 0.6)]

    def entity_domain(self, eid: str) -> Optional[str]:
        e = self.entities.get(eid)
        return e.domain if e else None

    # ------------------------------------------------------------------ expansion
    def expand_terms(self, text: str) -> list[str]:
        """English terms implied by the question: Turkish->English map + acronym expansions."""
        out: list[str] = []
        folded = fold_tr(text)
        for pat, english in self._terms:
            if pat.search(folded):
                out.append(english)
        for pat, acro, expansion in self._acronyms:
            if pat.search(text):
                out.append(f"{acro} {expansion}")
        return _dedupe(out)

    def glossary_hints(self, domain_keys: Iterable[str], text: str) -> list[str]:
        keys = set(domain_keys)
        hints = []
        for pat, acro, expansion in self._acronyms:
            if pat.search(text) and (not keys or any(acro in self.domains[k].glossary
                                                     for k in keys if k in self.domains)):
                hints.append(f"{acro} = {expansion}")
        return _dedupe(hints)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out
