"""Query planning: follow-up resolution, Turkish -> English translation and keyword expansion."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from techrag.domains import DomainRegistry
from techrag.llm import LLMClient, parse_json_object
from techrag.prompts import PLANNER_EXAMPLE_ASSISTANT, PLANNER_EXAMPLE_USER, PLANNER_SYSTEM

_TR_CHARS = set("çğıöşüÇĞİÖŞÜ")
_TR_WORDS = {"nedir", "nasıl", "nasil", "kaç", "kac", "hangi", "nelerdir", "midir", "mıdır", "neden", "için",
             "icin", "ile", "ve", "veya", "bir", "bu", "ne", "mi", "mı", "olan", "olarak", "değeri", "degeri",
             "arasındaki", "farkı", "peki", "nerede", "açıkla", "acikla", "göre", "gore", "kadar"}


def detect_language(text: str) -> str:
    if any(c in _TR_CHARS for c in text):
        return "tr"
    words = set(re.findall(r"\w+", text.lower()))
    return "tr" if len(words & _TR_WORDS) >= 1 else "en"


@dataclass
class QueryPlan:
    question: str
    standalone: str
    english: str
    language: str
    search_queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    expansions: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    rewritten_by_llm: bool = False

    def bm25_queries(self) -> list[str]:
        qs = [
            " ".join([self.standalone, *self.expansions]),
            self.english if self.english != self.standalone else "",
            " ".join(self.keywords),
            *self.search_queries[:3],
        ]
        return _dedupe(q for q in qs if q.strip())

    def dense_queries(self) -> list[str]:
        return _dedupe(q for q in [self.standalone, self.english, *self.search_queries[:3]] if q.strip())

    @property
    def rerank_query(self) -> str:
        return self.english or self.standalone

    def to_dict(self) -> dict:
        return asdict(self)


def _dedupe(items) -> list[str]:
    seen, out = set(), []
    for it in items:
        key = it.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out


def _history_text(history: Sequence[dict], max_turns: int) -> str:
    lines = []
    for m in list(history)[-2 * max_turns:]:
        role = m.get("role", "user")
        content = re.sub(r"\[\d+\]", "", str(m.get("content", "")))
        content = re.sub(r"\s+", " ", content).strip()
        if role == "assistant" and len(content) > 400:
            content = content[:400] + " ..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


class QueryPlanner:
    def __init__(self, domains: DomainRegistry, llm: Optional[LLMClient] = None, use_llm: bool = True,
                 history_turns: int = 3):
        self.domains = domains
        self.llm = llm
        self.use_llm = use_llm and llm is not None
        self.history_turns = history_turns
        self.last_error: Optional[str] = None

    def plan(self, question: str, history: Optional[Sequence[dict]] = None) -> QueryPlan:
        question = question.strip()
        history = list(history or [])
        self.last_error = None
        language = detect_language(question)
        plan = QueryPlan(question=question, standalone=question, english=question, language=language)

        if self.use_llm:
            try:
                self._llm_rewrite(plan, history)
            except Exception as exc:  # planning is an optimisation; never fail the question because of it
                self.last_error = f"{exc.__class__.__name__}: {exc}"

        text_for_terms = " ".join([plan.standalone, plan.question])
        plan.expansions = self.domains.expand_terms(text_for_terms)
        # Routing uses only explicit mentions in the user's own words (and the resolved standalone question),
        # never the model's guesses.
        plan.domains = self.domains.detect(" ".join([plan.question, plan.standalone]), exclude_general=False)
        if not plan.keywords:
            plan.keywords = _dedupe(plan.expansions)
        return plan

    def _llm_rewrite(self, plan: QueryPlan, history: list[dict]) -> None:
        hist = _history_text(history, self.history_turns)
        user = (f"History:\n{hist}\n" if hist else "History: (none)\n") + f"Question: {plan.question}"
        raw = self.llm.chat(
            [
                {"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": PLANNER_EXAMPLE_USER},
                {"role": "assistant", "content": PLANNER_EXAMPLE_ASSISTANT},
                {"role": "user", "content": user},
            ],
            json_mode=True, max_tokens=400, temperature=0.0,
        )
        obj = parse_json_object(raw)
        if not obj:
            raise ValueError(f"planner returned no JSON: {raw[:200]!r}")

        def text(key: str) -> str:
            val = obj.get(key)
            return val.strip() if isinstance(val, str) else ""

        def items(key: str, limit: int) -> list[str]:
            val = obj.get(key)
            if isinstance(val, str):
                val = [val]
            if not isinstance(val, list):
                return []
            return _dedupe(str(v) for v in val if isinstance(v, (str, int, float)))[:limit]

        if history and text("standalone_question"):
            plan.standalone = text("standalone_question")
        plan.english = text("english_question") or plan.standalone
        plan.search_queries = items("search_queries", 4)
        plan.keywords = items("keywords", 12)
        plan.rewritten_by_llm = True
