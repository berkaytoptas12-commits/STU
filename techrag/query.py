"""Query planning: follow-up resolution, Turkish -> English terminology, keywords, question type.

Standards used for the HARD filter come only from the user's own words (question + resolved standalone
question), never from the model's guesses.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional, Sequence

from techrag.clarify import COMPAT_KINDS, is_compatibility
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
- "search_queries": 2-5 short English search queries (different phrasings, expanded acronyms, likely section/table names). Standards state requirements in their own normative terms, not in the words of design practice. When the question is about a design choice or practice ("is X needed", how to route, lay out, terminate or choose a component, how long or how far), think which requirements of the standard govern it - limits, timing or electrical parameters the design must satisfy - and include queries for those in the standard's own terms (for example, a pull-up resistor value is governed by the rise time, bus capacitance and sink current limits).
- "keywords": 3-10 exact terms likely to appear verbatim (parameter/signal/register/state names, symbols, acronyms and expansions).
- "parameters": names or symbols of numeric parameters the question asks for or that govern it (e.g. ["tRFC", "VOD", "rise time"]), else [].
- "spec_phrasing": one or two sentences written the way the standard itself would state the requirement that answers or governs the question, in its normative terminology, with X in place of every number. Used only to find passages; never shown to the user.
- "question_type": one of lookup | numeric | comparison | procedure | multi_part | other.
- "needs_reasoning": true if answering requires combining several facts, comparing standards/versions, or calculations.
- "compatibility": true only if the user asks whether specific devices, signals or interfaces can be connected or work together electrically; else false.
- "missing_conditions": only for such a compatibility question: which of "voltage" (supply / I/O levels), "interface_role" (which side transmits/drives and which receives), "termination" the verdict depends on but are stated neither in the question nor in the history. [] for every other question, including general or conceptual ones.
Never answer the question. Never invent values."""

PLANNER_EXAMPLE_USER = """History:
user: DDR4 için tRFC nedir?
Question: Peki DDR5'te 16Gb yoğunluk için değeri ne?"""

PLANNER_EXAMPLE_ASSISTANT = """{"standalone_question": "DDR5'te 16Gb yoğunluklu bir cihaz için tRFC (Refresh Cycle Time) değeri nedir?", "english_question": "What is the tRFC (refresh cycle time) value for a 16Gb DDR5 SDRAM device?", "search_queries": ["DDR5 tRFC1 refresh cycle time 16Gb", "DDR5 refresh timing parameters by density table", "tRFC1 tRFC2 tRFCsb 16 Gb"], "keywords": ["tRFC", "tRFC1", "tRFC2", "tRFCsb", "Refresh Cycle Time", "16Gb", "REFab"], "parameters": ["tRFC1", "tRFC2", "tRFCsb"], "spec_phrasing": "The refresh cycle time tRFC1 of an X Gb DDR5 SDRAM device shall be at least X ns.", "question_type": "numeric", "needs_reasoning": false, "compatibility": false, "missing_conditions": []}"""


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
    compatibility: bool = False
    missing_conditions: list[str] = field(default_factory=list)   # compatibility conditions not stated
    carried_entities: list[str] = field(default_factory=list)     # standard taken over from the conversation
    spec_phrasing: str = ""         # how the standard would word the governing requirement (dense search only)

    def bm25_queries(self) -> list[str]:
        qs = [" ".join([self.standalone, *self.expansions]),
              self.english if self.english != self.standalone else "",
              " ".join(self.keywords), *self.search_queries[:5]]
        return _dedupe(q for q in qs if q.strip())

    def dense_queries(self) -> list[str]:
        return _dedupe(q for q in [self.standalone, self.english, *self.search_queries[:4], self.spec_phrasing]
                       if q.strip())

    @property
    def rerank_query(self) -> str:
        return self.english or self.standalone

    @property
    def wants_parameters(self) -> bool:
        return bool(self.parameters) or self.question_type in ("numeric", "comparison")

    def to_dict(self) -> dict:
        return asdict(self)


def _no_values(text: str) -> str:
    """A value the planner put into its phrasing anyway is replaced by X: it is a search aid, not a source."""
    from techrag.units import find_quantities

    for q in find_quantities(text):
        text = text.replace(q.text, "X", 1)
    return text


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
        if not plan.rewritten_by_llm:
            plan.compatibility = is_compatibility(question)
        if not plan.entities and history and plan.wants_parameters:
            self._carry_entities(plan, history)
        if len(plan.entities) > 1 and plan.question_type not in ("comparison", "multi_part"):
            plan.needs_reasoning = True
        if not plan.keywords:
            plan.keywords = _dedupe(plan.expansions)
        return plan

    def _carry_entities(self, plan: QueryPlan, history: list[dict]) -> None:
        """A value follow-up without a standard ("Peki tREFI?") keeps the standard the user named last in this
        conversation, unless the question points at another collection (a new topic)."""
        asked = set(self.domains.detect(f"{plan.question} {plan.standalone}", exclude_general=True))
        for m in reversed([m for m in history if m.get("role") == "user"][-self.history_turns:]):
            ents = self.domains.detect_entities(str(m.get("content", "")))
            if not ents:
                continue
            if asked and not asked & {self.domains.entity_domain(e) for e in ents}:
                return
            plan.entities = ents
            plan.carried_entities = list(ents)
            note = f" ({', '.join(ents)})"
            plan.standalone += note
            if plan.english:
                plan.english += note
            return

    def _llm_rewrite(self, plan: QueryPlan, history: list[dict]) -> None:
        hist = _history_text(history, self.history_turns)
        user = (f"History:\n{hist}\n" if hist else "History: (none)\n") + f"Question: {plan.question}"
        res = self.llm.chat(
            [{"role": "system", "content": PLANNER_SYSTEM},
             {"role": "user", "content": PLANNER_EXAMPLE_USER},
             {"role": "assistant", "content": PLANNER_EXAMPLE_ASSISTANT},
             {"role": "user", "content": user}],
            json_mode=True, max_tokens=700, temperature=0.0, thinking=False)
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
        plan.search_queries = items("search_queries", 5)
        plan.spec_phrasing = _no_values(text("spec_phrasing"))[:400]
        plan.keywords = items("keywords", 12)
        plan.parameters = items("parameters", 8)
        qt = text("question_type").lower()
        plan.question_type = qt if qt in QUESTION_TYPES else "other"
        plan.needs_reasoning = bool(obj.get("needs_reasoning")) or plan.question_type in ("comparison", "multi_part")
        plan.compatibility = obj.get("compatibility") is True
        plan.missing_conditions = [k for k in items("missing_conditions", 3) if k in COMPAT_KINDS] \
            if plan.compatibility else []
        plan.rewritten_by_llm = True
