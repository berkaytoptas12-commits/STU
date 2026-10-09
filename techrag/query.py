"""Query planning: follow-up resolution, Turkish -> English terminology, keywords, question type.

Standards used for the HARD filter come only from the user's own words (question + resolved standalone
question), never from the model's guesses.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from techrag.domains import DomainRegistry
from techrag.llm import LLMClient, parse_json_object

_TR_CHARS = set("çğıöşüÇĞİÖŞÜ")
_TR_WORDS = {"nedir", "nasıl", "nasil", "kaç", "kac", "hangi", "nelerdir", "midir", "mıdır", "neden", "için",
             "icin", "ile", "ve", "veya", "bir", "bu", "ne", "mi", "mı", "olan", "olarak", "değeri", "degeri",
             "arasındaki", "farkı", "peki", "nerede", "açıkla", "acikla", "göre", "gore", "kadar"}
QUESTION_TYPES = ("lookup", "numeric", "comparison", "procedure", "multi_part", "other")

PLANNER_SYSTEM = """You turn a user's question into a retrieval plan for a library of hardware interface and design standards (ARINC, JEDEC DDR/LPDDR, PCI Express, IEEE 802.3 Ethernet, DisplayPort, USB/Type-C/PD, TIA-422/485, I2C/SMBus/I3C, DO-254/DO-160/MIL-STD/IPC ...). The documents are in English; the user may write Turkish or English and may ask follow-ups that depend on the history.

Return ONLY a JSON object with these keys:
- "standalone_question": the question made fully self-contained using the history, in the SAME language as the user.
- "english_question": faithful English version using the exact terminology of the relevant standard.
- "search_queries": 2-4 short English search queries (different phrasings, expanded acronyms, likely section/table names).
- "keywords": 3-10 exact terms likely to appear verbatim (parameter/signal/register/state names, symbols, acronyms and expansions).
- "parameters": names or symbols of numeric parameters the question asks for (e.g. ["tRFC", "VOD", "rise time"]), else [].
- "question_type": one of lookup | numeric | comparison | procedure | multi_part | other.
- "needs_reasoning": true if answering requires combining several facts, comparing standards/versions, or calculations.
Never answer the question. Never invent values."""

PLANNER_EXAMPLE_USER = """History:
user: DDR4 için tRFC nedir?
Question: Peki DDR5'te 16Gb yoğunluk için değeri ne?"""

PLANNER_EXAMPLE_ASSISTANT = """{"standalone_question": "DDR5'te 16Gb yoğunluklu bir cihaz için tRFC (Refresh Cycle Time) değeri nedir?", "english_question": "What is the tRFC (refresh cycle time) value for a 16Gb DDR5 SDRAM device?", "search_queries": ["DDR5 tRFC1 refresh cycle time 16Gb", "DDR5 refresh timing parameters by density table", "tRFC1 tRFC2 tRFCsb 16 Gb"], "keywords": ["tRFC", "tRFC1", "tRFC2", "tRFCsb", "Refresh Cycle Time", "16Gb", "REFab"], "parameters": ["tRFC1", "tRFC2", "tRFCsb"], "question_type": "numeric", "needs_reasoning": false}"""


def detect_language(text: str) -> str:
    if any(c in _TR_CHARS for c in text):
        return "tr"
    words = set(re.findall(r"\w+", text.lower()))
    return "tr" if words & _TR_WORDS else "en"


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
    entities: list[str] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    question_type: str = "other"
    needs_reasoning: bool = False
    rewritten_by_llm: bool = False

    def bm25_queries(self) -> list[str]:
        qs = [" ".join([self.standalone, *self.expansions]),
              self.english if self.english != self.standalone else "",
              " ".join(self.keywords), *self.search_queries[:3]]
        return _dedupe(q for q in qs if q.strip())

    def dense_queries(self) -> list[str]:
        return _dedupe(q for q in [self.standalone, self.english, *self.search_queries[:3]] if q.strip())

    @property
    def rerank_query(self) -> str:
        return self.english or self.standalone

    @property
    def wants_parameters(self) -> bool:
        return bool(self.parameters) or self.question_type in ("numeric", "comparison")

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
        content = re.sub(r"\[\d+\]", "", str(m.get("content", "")))
        content = re.sub(r"\s+", " ", content).strip()
        if m.get("role") == "assistant" and len(content) > 400:
            content = content[:400] + " ..."
        lines.append(f"{m.get('role', 'user')}: {content}")
    return "\n".join(lines)


_NUMERIC_HINT = re.compile(r"\b(kaç|kac|değer|deger|maksimum|minimum|max|min|typ|how (?:much|many|long|fast)|"
                           r"what is the (?:value|maximum|minimum)|voltage|gerilim|süre|sure|frekans|frequency|"
                           r"rate|hız|hiz|timing|zamanlama)\b", re.I)
_COMPARE_HINT = re.compile(r"\b(fark|karşılaştır|karsilastir|vs\.?|versus|compare|comparison|difference|"
                           r"arasında|between)\b", re.I)


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
        plan = QueryPlan(question=question, standalone=question, english=question,
                         language=detect_language(question))
        if self.use_llm:
            try:
                self._llm_rewrite(plan, history)
            except Exception as exc:  # planning is an optimisation; never fail the question because of it
                self.last_error = f"{exc.__class__.__name__}: {exc}"
        if not plan.rewritten_by_llm:
            if _COMPARE_HINT.search(question):
                plan.question_type, plan.needs_reasoning = "comparison", True
            elif _NUMERIC_HINT.search(question):
                plan.question_type = "numeric"
        explicit = " ".join([plan.question, plan.standalone])
        plan.expansions = self.domains.expand_terms(explicit)
        plan.domains = self.domains.detect(explicit)
        plan.entities = self.domains.detect_entities(explicit)
        if len(plan.entities) > 1 and plan.question_type not in ("comparison", "multi_part"):
            plan.needs_reasoning = True
        if not plan.keywords:
            plan.keywords = _dedupe(plan.expansions)
        return plan

    def _llm_rewrite(self, plan: QueryPlan, history: list[dict]) -> None:
        hist = _history_text(history, self.history_turns)
        user = (f"History:\n{hist}\n" if hist else "History: (none)\n") + f"Question: {plan.question}"
        res = self.llm.chat(
            [{"role": "system", "content": PLANNER_SYSTEM},
             {"role": "user", "content": PLANNER_EXAMPLE_USER},
             {"role": "assistant", "content": PLANNER_EXAMPLE_ASSISTANT},
             {"role": "user", "content": user}],
            json_mode=True, max_tokens=500, temperature=0.0, thinking=False)
        obj = parse_json_object(res.content)
        if not obj:
            raise ValueError(f"planner returned no JSON: {res.content[:200]!r}")

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
        plan.parameters = items("parameters", 8)
        qt = text("question_type").lower()
        plan.question_type = qt if qt in QUESTION_TYPES else "other"
        plan.needs_reasoning = bool(obj.get("needs_reasoning")) or plan.question_type in ("comparison", "multi_part")
        plan.rewritten_by_llm = True
