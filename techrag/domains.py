"""Standard families ("collections"): detection, folder mapping and query term expansion."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

_TR_FOLD = str.maketrans({
    "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "I": "i", "İ": "i",
    "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u", "â": "a", "î": "i", "û": "u",
})


def fold_tr(text: str) -> str:
    """Lowercase + fold Turkish letters to ASCII so 'Hızı', 'hizi', 'HIZI' all compare equal."""
    return text.translate(_TR_FOLD).lower()


@dataclass
class Domain:
    key: str
    name: str
    folders: list[str] = field(default_factory=list)
    patterns: list[re.Pattern] = field(default_factory=list)
    glossary: dict[str, str] = field(default_factory=dict)


class DomainRegistry:
    def __init__(self, domains: dict[str, Domain], term_map: dict[str, str]):
        self.domains = domains
        self.term_map = term_map
        self._folder_index = {f.lower(): d.key for d in domains.values() for f in [d.key, *d.folders]}
        # Turkish term map: matched on folded text, anchored at a word start. Long stems may take any
        # suffix ("zamanlamaları"); short ones only common inflections, so "yük" does not hit "yüksek".
        self._terms = [(self._term_pattern(fold_tr(k)), v) for k, v in term_map.items()]
        self._acronyms: list[tuple[re.Pattern, str, str]] = []
        for d in domains.values():
            for acro, expansion in d.glossary.items():
                flags = 0 if len(acro) <= 3 else re.IGNORECASE
                pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(acro) + r"(?![A-Za-z0-9])", flags)
                self._acronyms.append((pat, acro, expansion))

    _SHORT_SUFFIXES = ("i", "u", "e", "a", "si", "su", "yi", "yu", "ye", "ya", "in", "un", "nin", "nun",
                       "de", "da", "te", "ta", "den", "dan", "ten", "tan", "ler", "lar", "leri", "lari")

    # Final-consonant softening before a vowel suffix: uzunluk -> uzunluğu, tip -> tibi, ...
    _SOFTEN = {"k": "[kg]", "p": "[pb]", "t": "[td]"}

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
            )
        if "general" not in domains:
            domains["general"] = Domain(key="general", name="General")
        term_map = {str(k): str(v) for k, v in (raw.get("term_map") or {}).items()}
        return cls(domains, term_map)

    # ---------------------------------------------------------------- detection
    def keys(self) -> list[str]:
        return list(self.domains)

    def name(self, key: str) -> str:
        d = self.domains.get(key)
        return d.name if d else key

    def domain_for_folder(self, folder: str) -> str | None:
        return self._folder_index.get(folder.lower())

    def score(self, text: str) -> dict[str, int]:
        scores: dict[str, int] = {}
        for d in self.domains.values():
            n = sum(len(p.findall(text)) for p in d.patterns)
            if n:
                scores[d.key] = n
        return scores

    def detect(self, text: str, exclude_general: bool = False) -> list[str]:
        """Domains explicitly referenced in a question, strongest first."""
        scores = self.score(text)
        if exclude_general:
            scores.pop("general", None)
        return [k for k, _ in sorted(scores.items(), key=lambda kv: -kv[1])]

    def classify_document(self, rel_path: Path, sample_text: str) -> str:
        """Collection of a source file: folder name first, then filename, then content keywords."""
        for part in rel_path.parts[:-1]:
            hit = self.domain_for_folder(part)
            if hit:
                return hit
        name_hits = self.detect(rel_path.stem.replace("_", " "))
        if name_hits:
            return name_hits[0]
        content = self.score(sample_text)
        if content:
            best, n = max(content.items(), key=lambda kv: kv[1])
            if n >= 3:
                return best
        return "general"

    # ---------------------------------------------------------------- expansion
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
        """Glossary lines relevant to the question (shown to the LLM as terminology hints)."""
        hints = []
        keys = set(domain_keys)
        for pat, acro, expansion in self._acronyms:
            if pat.search(text) and (not keys or any(acro in self.domains[k].glossary for k in keys)):
                hints.append(f"{acro} = {expansion}")
        return _dedupe(hints)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out
