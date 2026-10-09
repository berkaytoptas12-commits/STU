"""Agent tools and the citation registry they share.

Every piece of evidence the model sees (passage, parameter row, table, page, calculation) gets a stable
number [n] in one registry, so citations can be checked against exactly what the model was shown.
"""

from __future__ import annotations

import ast
import math
import operator
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from techrag.ingest.chunker import estimate_tokens
from techrag.query import QueryPlan
from techrag.retrieval import Passage, Retriever
from techrag.store import ParameterRow, Store, TableRow

TOOL_RESULT_TOKENS = 2500


@dataclass
class Source:
    n: int
    kind: str                 # passage | parameter | table | page | calc
    text: str
    doc_id: Optional[int] = None
    doc_title: str = ""
    domain: str = ""
    section: str = ""
    page_start: int = 0
    page_end: int = 0
    entities: list[str] = field(default_factory=list)
    doc_type: str = ""
    revision: str = ""
    score: float = 0.0
    verified: bool = True
    key: str = ""

    def header(self) -> str:
        pages = f"p. {self.page_start}" if self.page_start == self.page_end else f"pp. {self.page_start}-{self.page_end}"
        if self.kind == "calc":
            return f"[{self.n}] Calculation"
        bits = [f"[{self.n}]"]
        if self.entities:
            bits.append(f"Standard: {', '.join(self.entities)}")
        bits.append(f"Document: {self.doc_title}" + (f" (rev {self.revision})" if self.revision else ""))
        if self.doc_type and self.doc_type != "base":
            bits.append(f"Type: {self.doc_type.upper()} (overrides the base document)")
        if self.section:
            bits.append(f"Section: {self.section}")
        bits.append(pages)
        if self.kind != "passage":
            bits.append(f"({self.kind}{'' if self.verified else ', UNVERIFIED extraction'})")
        return " | ".join(bits)

    def prompt_text(self) -> str:
        return f"{self.header()}\n{self.text}"

    def to_dict(self) -> dict:
        return asdict(self)


class SourceRegistry:
    def __init__(self):
        self.items: list[Source] = []
        self._keys: dict[str, Source] = {}

    def __len__(self) -> int:
        return len(self.items)

    def get(self, n: int) -> Optional[Source]:
        return self.items[n - 1] if 1 <= n <= len(self.items) else None

    def _add(self, key: str, **kw) -> tuple[Source, bool]:
        if key in self._keys:
            return self._keys[key], False
        src = Source(n=len(self.items) + 1, key=key, **kw)
        self.items.append(src)
        self._keys[key] = src
        return src, True

    def add_passage(self, p: Passage) -> tuple[Source, bool]:
        key = "c:" + ",".join(map(str, p.chunk_ids))
        return self._add(key, kind="table" if p.kind == "table" else "passage", text=p.text, doc_id=p.doc_id,
                         doc_title=p.doc_title, domain=p.domain, section=p.section, page_start=p.page_start,
                         page_end=p.page_end, entities=p.entities, doc_type=p.doc_type, revision=p.revision,
                         score=p.score)

    def add_parameter(self, r: ParameterRow, doc_meta: dict) -> tuple[Source, bool]:
        caption = f"{r.caption} — " if r.caption else ""
        return self._add(f"p:{r.id}", kind="parameter", text=caption + r.line(), doc_id=r.doc_id,
                         doc_title=r.doc_title, domain=r.domain, section=r.section, page_start=r.page,
                         page_end=r.page, verified=r.verified, **doc_meta)

    def add_table(self, t: TableRow, doc_meta: dict) -> tuple[Source, bool]:
        text = t.markdown
        if estimate_tokens(text) > TOOL_RESULT_TOKENS:
            text = text[: TOOL_RESULT_TOKENS * 4] + "\n... (table truncated; use get_parameter for specific rows)"
        return self._add(f"t:{t.id}", kind="table", text=text, doc_id=t.doc_id, doc_title=t.doc_title,
                         section=t.section, page_start=t.page, page_end=t.page,
                         verified=t.verified_ratio >= 0.8, **doc_meta)

    def add_page(self, doc_id: int, page: int, title: str, text: str, doc_meta: dict) -> tuple[Source, bool]:
        return self._add(f"g:{doc_id}:{page}", kind="page", text=text or "(page image)", doc_id=doc_id,
                         doc_title=title, page_start=page, page_end=page, **doc_meta)

    def add_calc(self, expression: str, result: str) -> tuple[Source, bool]:
        return self._add(f"x:{expression}", kind="calc", text=f"{expression} = {result}")

    def text_of(self, numbers) -> str:
        return "\n".join(s.text for n in numbers if (s := self.get(n)))

    def all_text(self) -> str:
        return "\n".join(s.text for s in self.items)


# --------------------------------------------------------------------------------- calculator

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "log10": math.log10, "log": math.log, "ln": math.log, "log2": math.log2,
          "exp": math.exp, "abs": abs, "min": min, "max": max, "round": round, "ceil": math.ceil,
          "floor": math.floor, "sin": math.sin, "cos": math.cos, "tan": math.tan, "atan": math.atan}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_eval(expression: str) -> float:
    """Arithmetic only: numbers, + - * / ** %, parentheses, a few math functions. No names, no attributes."""
    tree = ast.parse(expression.replace("^", "**").replace("×", "*").replace("·", "*"), mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"unsupported expression element: {ast.dump(node)[:60]}")

    if len(expression) > 300:
        raise ValueError("expression too long")
    return float(ev(tree))


# ------------------------------------------------------------------------------------- tools

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "search_docs",
        "description": "Search the loaded standard documents. Returns numbered source passages to cite.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "English search query using the standard's terminology"},
            "standard": {"type": "string", "description": "Optional standard/version to restrict to, e.g. 'DDR5', 'PCIe 5.0', 'ARINC 429'"},
            "top_k": {"type": "integer", "description": "Number of passages (1-8)", "default": 5}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_parameter",
        "description": "Look up typed parameter rows (min/typ/max/unit/conditions/notes) extracted from the standards' tables.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Parameter name or symbol, e.g. 'tRFC', 'VOD', 'rise time'"},
            "standard": {"type": "string", "description": "Optional standard/version, e.g. 'DDR4'"},
            "conditions": {"type": "string", "description": "Optional conditions, e.g. 'DDR4-3200', '8Gb', 'Fast-mode'"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "get_table",
        "description": "Fetch a whole table by its caption or topic (e.g. 'Table 4-12', 'refresh parameters').",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "standard": {"type": "string", "description": "Optional standard/version"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_page_image",
        "description": "Look at a page image (figures, timing diagrams, pinouts, waveforms). Give either the number of an existing source or a document id and page.",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "integer", "description": "Number n of an existing source [n]"},
            "document_id": {"type": "integer"},
            "page": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression (+ - * / ** sqrt log10 ln exp min max ...). Use SI numbers, e.g. '1/(400e3)' or '0.2*1.25'. Returns a citable result.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string"},
            "label": {"type": "string", "description": "What is being computed, with units"}},
            "required": ["expression"]}}},
]


@dataclass
class ToolOutput:
    text: str                            # tool message content for the model
    new_sources: list[Source] = field(default_factory=list)
    image_png: Optional[bytes] = None    # attached as a user message (tool messages cannot carry images)
    summary: str = ""


class ToolExecutor:
    def __init__(self, store: Store, retriever: Retriever, registry: SourceRegistry, plan: QueryPlan,
                 render_page: Callable[[int, int], Optional[bytes]], vision_input: bool = True,
                 base_doc_ids: Optional[list[int]] = None,
                 expand: Callable[[str], list[str]] = lambda q: [],
                 default_doc_ids: Optional[list[int]] = None):
        self.expand = expand
        self.default_doc_ids = default_doc_ids  # the question's own (entity) scope
        self.store = store
        self.retriever = retriever
        self.registry = registry
        self.plan = plan
        self.render_page = render_page
        self.vision_input = vision_input
        self.base_doc_ids = base_doc_ids  # user scope (UI selection) still applies inside tools

    def _doc_meta(self, doc_id: int) -> dict:
        d = self.retriever.catalog.docs().get(doc_id)
        if not d:
            return {}
        return {"entities": list(d.entities), "doc_type": d.doc_type, "revision": d.revision}

    def _scope_ids(self, standard: str) -> tuple[Optional[list[int]], str]:
        """(doc ids, note). The user's selection always wins; an explicit standard narrows to it; otherwise
        tools stay inside the question's scope (so a DDR5 question cannot pull DDR4 rows by accident)."""
        if self.base_doc_ids:
            return self.base_doc_ids, ""
        standard = (standard or "").strip()
        if standard:
            scope = self.retriever.catalog.resolve_scope(None, standard=standard, entity_filter=True,
                                                         domain_routing=False)
            if scope.reason == "entity":
                return scope.doc_ids, ""
            return scope.doc_ids, f"Note: no loaded document is tagged '{standard}'; searched all documents.\n\n"
        return self.default_doc_ids, ""

    def _format(self, sources: list[Source], empty: str) -> str:
        if not sources:
            return empty
        out, used = [], 0
        for s in sources:
            t = s.prompt_text()
            used += estimate_tokens(t)
            if out and used > TOOL_RESULT_TOKENS * 2:
                break
            out.append(t)
        return "\n\n---\n\n".join(out)

    def run(self, name: str, args: dict) -> ToolOutput:
        try:
            fn = getattr(self, f"tool_{name}", None)
            if fn is None:
                return ToolOutput(f"Unknown tool '{name}'.", summary=f"unknown tool {name}")
            return fn(**{k: v for k, v in args.items() if v not in (None, "")})
        except TypeError as exc:
            return ToolOutput(f"Invalid arguments for {name}: {exc}", summary="invalid arguments")
        except Exception as exc:
            return ToolOutput(f"Tool {name} failed: {exc}", summary=f"error: {exc}")

    def tool_search_docs(self, query: str, standard: str = "", top_k: int = 5) -> ToolOutput:
        top_k = max(1, min(int(top_k or 5), 8))
        plan = QueryPlan(question=query, standalone=query, english=query, language="en",
                         keywords=[], expansions=self.expand(query))
        ids, note = self._scope_ids(standard)
        res = self.retriever.search(plan, doc_ids=ids, top_k=top_k, budget_tokens=TOOL_RESULT_TOKENS * 2,
                                    with_parameters=False)
        new, shown = [], []
        for p in res.passages:
            src, is_new = self.registry.add_passage(p)
            shown.append(src)
            if is_new:
                new.append(src)
        return ToolOutput(note + self._format(shown, "No matching passages."), new,
                          summary=f"{len(shown)} passage(s) for '{query}'" + (f" in {standard}" if standard else ""))

    def tool_get_parameter(self, name: str, standard: str = "", conditions: str = "") -> ToolOutput:
        ids, note = self._scope_ids(standard)
        rows = self.store.search_parameters(f"{name} {conditions}", 10, ids, verified_only=False)
        rows.sort(key=lambda r: (not r.verified,))
        shown, new = [], []
        for r in rows[:10]:
            src, is_new = self.registry.add_parameter(r, self._doc_meta(r.doc_id))
            shown.append(src)
            if is_new:
                new.append(src)
        return ToolOutput(note + self._format(shown, f"No parameter rows found for '{name}'. Try search_docs."), new,
                          summary=f"{len(shown)} parameter row(s) for '{name}'")

    def tool_get_table(self, query: str, standard: str = "") -> ToolOutput:
        ids, note = self._scope_ids(standard)
        tables = self.store.tables_for(query=query, doc_ids=ids, limit=2)
        shown, new = [], []
        for t in tables:
            src, is_new = self.registry.add_table(t, self._doc_meta(t.doc_id))
            shown.append(src)
            if is_new:
                new.append(src)
        return ToolOutput(note + self._format(shown, f"No extracted table matches '{query}'. Try search_docs."), new,
                          summary=f"{len(shown)} table(s) for '{query}'")

    def tool_get_page_image(self, source: int = 0, document_id: int = 0, page: int = 0) -> ToolOutput:
        if source:
            s = self.registry.get(int(source))
            if not s or s.doc_id is None:
                return ToolOutput(f"Source [{source}] has no page.", summary="no page")
            document_id, page = s.doc_id, s.page_start
        if not document_id or not page:
            return ToolOutput("Give a source number, or document_id and page.", summary="missing arguments")
        doc = self.store.document(int(document_id))
        if not doc or not (1 <= int(page) <= max(doc.n_pages, 1)):
            return ToolOutput("No such document/page.", summary="no such page")
        text = "\n".join(c.text for c in self.store.page_chunks(doc.id, int(page)))[:4000]
        src, is_new = self.registry.add_page(doc.id, int(page), doc.title, text, self._doc_meta(doc.id))
        png = self.render_page(doc.id, int(page)) if self.vision_input else None
        note = (f"Page image of '{doc.title}' p. {page} is attached in the next message; cite it as [{src.n}]."
                if png else f"Text of '{doc.title}' p. {page} (image input unavailable), cite as [{src.n}]:\n{text}")
        return ToolOutput(note, [src] if is_new else [], png, summary=f"page {page} of {doc.title}")

    def tool_calculate(self, expression: str, label: str = "") -> ToolOutput:
        value = safe_eval(expression)
        result = f"{value:.10g}"
        expr = f"{label}: {expression}" if label else expression
        src, is_new = self.registry.add_calc(expr, result)
        return ToolOutput(f"[{src.n}] {expr} = {result}", [src] if is_new else [], summary=f"{expression} = {result}")
