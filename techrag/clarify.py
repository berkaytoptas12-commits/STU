"""Clarifying questions for conditions that change the answer.

A question is asked back only when the answer really depends on something the user did not state:
- standard/version: no standard is named (in the question, the conversation or the document selection) and the
  evidence for a value or compatibility question comes from sibling standards/versions of one collection;
- speed grade / density / operating condition: the verified parameter rows of the asked parameter give different
  values for different conditions and the question names none of them;
- voltage / TX-RX roles / termination: the planner marks an electrical compatibility question whose verdict
  depends on them and the text does not state them.
General or conceptual questions are answered directly. At most three short questions are asked; the offered
options come only from the index (loaded standards, conditions printed in verified table rows), never from the
model. The clarification text is a fixed template plus those index values: it carries no technical claim, so it
needs no verification and cannot be used to pass an unverified statement.

The UI sends the pending clarification back with the next message. A short reply ("DDR4, 2400 MT/s") is merged
into the original question; a new question (a different topic) drops it; "bilmiyorum" / "I don't know" gives a
source-supported conditional answer instead of another question.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from techrag.domains import fold_tr

MAX_QUESTIONS = 3
MAX_OPTIONS = 6
MAX_ROUNDS = 2          # after two clarifications in a row the question is answered conditionally
COMPAT_KINDS = ("voltage", "interface_role", "termination")
KINDS = ("standard_version", "speed_grade", "density", "temperature", "operating_condition") + COMPAT_KINDS

TEXTS = {
    "tr": {
        "intro": "Cevap, soruda belirtilmeyen koşullara göre değişiyor. Lütfen belirtin:",
        "intro_one": "Cevap, soruda belirtilmeyen bir koşula göre değişiyor.",
        "outro": "Seçeneklerden birini seçebilir ya da cevabı yazabilirsiniz. Bilmiyorsanız «bilmiyorum» yazın; "
                 "cevap koşullara bağlı olarak, kaynaklarda yazdığı kadarıyla açıklanır.",
        "standard_version": "Hangi standardı/sürümü kastediyorsunuz? Kaynaklarda değerler sürüme göre farklı "
                            "olabilir (yüklü: {options}).",
        "speed_grade": "{param} hangi hız sınıfı (speed grade) için? Kaynaklarda farklı değerler var ({options}).",
        "density": "{param} hangi yoğunluk (density) için? Kaynaklarda farklı değerler var ({options}).",
        "temperature": "{param} hangi sıcaklık aralığı için? Kaynaklarda farklı değerler var ({options}).",
        "operating_condition": "{param} hangi çalışma koşulu için? Kaynaklarda farklı değerler var ({options}).",
        "voltage": "Bağlanacak arayüzlerin besleme / G/Ç gerilim seviyeleri nedir?",
        "interface_role": "Hangi taraf verici (TX / sürücü), hangi taraf alıcı (RX)?",
        "termination": "Hatta hangi sonlandırma (termination) kullanılıyor ya da kullanılacak?",
        "all_versions": "Hepsini karşılaştır",
        "all_conditions": "Tümünü listele",
        "all_conditions_value": "tüm koşullar için",
        "dont_know": "bilmiyorum",
        "conditional": "Not: {conditions} belirtilmediği için kesin bir değer veya uyumluluk kararı verilmedi; "
                       "aşağıdaki açıklama kaynaklarda yazan koşullara bağlıdır.",
        "kind_standard_version": "standart/sürüm", "kind_speed_grade": "hız sınıfı", "kind_density": "yoğunluk",
        "kind_temperature": "sıcaklık aralığı", "kind_operating_condition": "çalışma koşulu",
        "kind_voltage": "gerilim seviyeleri", "kind_interface_role": "TX/RX rolleri",
        "kind_termination": "sonlandırma",
    },
    "en": {
        "intro": "The answer depends on conditions the question does not state. Please specify:",
        "intro_one": "The answer depends on a condition the question does not state.",
        "outro": "Pick an option or type the answer. If you do not know, type \"I don't know\": the answer is then "
                 "explained per condition, as far as the sources state it.",
        "standard_version": "Which standard/version do you mean? Values may differ between versions (loaded: "
                            "{options}).",
        "speed_grade": "Which speed grade is {param} for? The sources give different values ({options}).",
        "density": "Which density is {param} for? The sources give different values ({options}).",
        "temperature": "Which temperature range is {param} for? The sources give different values ({options}).",
        "operating_condition": "Which operating condition is {param} for? The sources give different values "
                               "({options}).",
        "voltage": "What are the supply / I/O voltage levels of the interfaces to be connected?",
        "interface_role": "Which side transmits (TX / driver) and which side receives (RX)?",
        "termination": "Which termination is (or will be) used on the line?",
        "all_versions": "Compare all",
        "all_conditions": "List all",
        "all_conditions_value": "for all conditions",
        "dont_know": "I don't know",
        "conditional": "Note: {conditions} not given, so no single value or compatibility verdict is stated; "
                       "the explanation below depends on the conditions written in the sources.",
        "kind_standard_version": "standard/version", "kind_speed_grade": "speed grade", "kind_density": "density",
        "kind_temperature": "temperature range", "kind_operating_condition": "operating condition",
        "kind_voltage": "voltage levels", "kind_interface_role": "TX/RX roles", "kind_termination": "termination",
    },
}


def text(lang: str, key: str, **kw) -> str:
    return TEXTS.get(lang, TEXTS["en"])[key].format(**kw)


def kind_labels(lang: str, kinds: Sequence[str]) -> str:
    return ", ".join(text(lang, f"kind_{k}") for k in dict.fromkeys(kinds) if k in KINDS)


# ------------------------------------------------------------------------------------------- text checks
_SPEED = re.compile(r"\b\d{3,5}\s*(?:mt/s|mbps|gt/s|mhz)\b|\b(?:lp)?ddr\d?x?[\s-]\d{3,4}\b|speed\s*(?:bin|grade)|"
                    r"(?<![\w.])-\d{4}[a-z]?\b", re.I)
_DENSITY = re.compile(r"\b\d+\s*gb(?:it)?\b(?!/s)", re.I)
_TEMP = re.compile(r"°\s*c\b|\btc(?:ase)?\b|temperature|sıcaklık|\bcommercial\b|\bindustrial\b|\bextended\b|"
                   r"\bautomotive\b", re.I)
_ALL = re.compile(r"\b(?:tüm|tümü|hepsi\w*|bütün|her\s+bir|listele\w*|tablo\w*|all|every|each|list|table)\b", re.I)
_COMPAT = re.compile(r"uyumlu|uyumluluk|bağla(?:nabilir|yabilir|nır|mak|rsam)|sür(?:ebilir|er)\s+mi|"
                     r"compatib|interoperab|\bconnect|\bdrive\b|\bdirectly\b", re.I)
_GIVEN = {
    "voltage": re.compile(r"\d+(?:[.,]\d+)?\s*m?v\b|\blv(?:cmos|ttl|ds|pecl)\w*|\b(?:sstl|hstl|pod|ttl|cmos|"
                          r"pecl|cml)\w*|gerilim\w*\s*[:=]", re.I),
    "interface_role": re.compile(r"\b(?:tx|rx|transmitter|receiver|driver|verici\w*|alıcı\w*|sürücü\w*|çıkış\w*|"
                                 r"giriş\w*|output\w*|input\w*)\b", re.I),
    "termination": re.compile(r"sonland|terminat|\d+\s*(?:Ω|ohm)|\bodt\b|pull[\s-]?(?:up|down)|"
                              r"(?:ac|dc)[\s-]?coupl|kuplaj", re.I),
}
_UNKNOWN = re.compile(r"\b(?:bilmiyorum|bilmiyoruz|emin değilim|emin degilim|bilinmiyor|belli değil|fark etmez|"
                      r"i don'?t know|do not know|not sure|unknown|no idea)\b", re.I)
_QUESTION = re.compile(r"\b(?:nedir|nelerdir|nasıl|nasil|neden|niçin|kaç|kac|ne zaman|nerede|açıkla\w*|"
                       r"what|how|why|which|when|where|explain|describe)\b", re.I)


def condition_kind(cond: str) -> str:
    if _SPEED.search(cond):
        return "speed_grade"
    if _DENSITY.search(cond):
        return "density"
    if _TEMP.search(cond):
        return "temperature"
    return "operating_condition"


_UNIT = r"(?:mt/s|gt/s|gb/s|mb/s|mbps|gbps|mhz|ghz|gbit|gb|mb|kb|°c|ns|ps|us|µs|ms|mv|v|ma|a|ohm|ω)"
_VALUE = re.compile(r"(?<![a-z\d.,])(\d+(?:[.,]\d+)?)\s?(" + _UNIT + r")?(?![a-z\d])")
_FILLER = {"with", "only", "when", "from", "speed", "grade", "bins", "device", "devices", "case", "range", "than",
           "less", "more", "above", "below"}


def _tokens(cond: str) -> list[str]:
    """The distinctive parts of a condition: values with their unit ('8gb', '85°c'), numbers of three or more
    digits ('2400' also matches 'DDR4-2400' and '2400 MT/s') and longer words. A digit glued to a name
    ('DDR4') is not a condition value."""
    low = fold_tr(cond)
    out = []
    for num, unit in _VALUE.findall(low):
        digits = re.sub(r"\D", "", num)
        if unit:
            out.append(num + unit)
        if len(digits) >= 3 or (not unit and len(digits) >= 2):
            out.append(num)
    out += [w for w in re.findall(r"[a-z]{4,}", low) if w not in _FILLER]
    return list(dict.fromkeys(out))


def mentions(text_: str, cond: str) -> bool:
    """Does the text already name this condition?"""
    squeezed = re.sub(r"\s+", "", fold_tr(text_))
    spaced = fold_tr(text_)
    for tok in _tokens(cond):
        if re.fullmatch(r"[a-z]+", tok):
            if re.search(r"\b" + re.escape(tok) + r"\b", spaced):
                return True
        elif re.search(r"(?<![\d.])" + re.escape(tok) + r"(?![\d])", squeezed):
            return True
    return False


def asks_all(text_: str) -> bool:
    return bool(_ALL.search(text_))


def is_compatibility(text_: str) -> bool:
    return bool(_COMPAT.search(text_))


def stated(kind: str, text_: str) -> bool:
    pat = _GIVEN.get(kind)
    return bool(pat and pat.search(text_))


# ---------------------------------------------------------------------------------------- the questions
@dataclass
class Question:
    kind: str
    text: str
    options: list[dict] = field(default_factory=list)   # {"label", "value"}; from the index only

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "options": self.options}


def version_question(lang: str, entities: dict[str, list[str]]) -> Question:
    """entities: standard id -> titles of its loaded documents (as found in the evidence)."""
    ents = sorted(entities)
    opts = [{"label": f"{e} — {', '.join(entities[e][:2])}", "value": e} for e in ents]
    opts.append({"label": text(lang, "all_versions"), "value": " vs ".join(ents), "all": True})
    return Question("standard_version", text(lang, "standard_version", options=", ".join(ents)), opts)


def condition_question(lang: str, kind: str, param: str, conditions: list[str]) -> Question:
    conds = [c[:120] for c in conditions[:MAX_OPTIONS]]
    opts = [{"label": c if len(c) <= 80 else c[:77] + "...", "value": c} for c in conds]
    opts.append({"label": text(lang, "all_conditions"), "value": text(lang, "all_conditions_value"), "all": True})
    return Question(kind, text(lang, kind, param=param, options="; ".join(conds)), opts)


def compat_question(lang: str, kind: str) -> Question:
    return Question(kind, text(lang, kind))


def varying_conditions(rows: Sequence, params: Sequence[str]) -> list[tuple[str, str, list[str]]]:
    """(kind, parameter, conditions) for each asked parameter whose verified rows give different values for
    different conditions of ONE document set (rows are grouped per standard and symbol)."""
    wanted = [p.lower() for p in params if p.strip()]
    if not wanted:
        return []
    groups: dict[tuple, list] = {}
    for r in rows:
        if not getattr(r, "verified", False):
            continue
        sym = (r.symbol or r.parameter or "").strip()
        name = (r.parameter or "").lower()
        if not sym or not any(sym.lower() == w or sym.lower().startswith(w) or (len(w) > 3 and w in name)
                              for w in wanted):
            continue
        groups.setdefault((r.doc_id, sym.lower()), []).append(r)
    out = []
    for (_, sym), rs in groups.items():
        by_cond: dict[str, tuple] = {}
        for r in rs:
            cond = re.sub(r"\s+", " ", (r.conditions or "")).strip()
            if not cond:
                continue
            by_cond.setdefault(cond, (r.min.strip(), r.typ.strip(), r.max.strip(), r.unit.strip()))
        if len(by_cond) >= 2 and len(set(by_cond.values())) >= 2:
            conds = list(by_cond)
            kinds = [condition_kind(c) for c in conds]
            kind = max(set(kinds), key=kinds.count)
            out.append((kind, rs[0].symbol or rs[0].parameter, conds))
    return out


def build(lang: str, questions: list[Question], pending: dict) -> Optional[dict]:
    if not questions:
        return None
    qs = questions[:MAX_QUESTIONS]
    if len(qs) == 1:
        body = f"{text(lang, 'intro_one')} {qs[0].text}"
    else:
        body = text(lang, "intro") + "\n\n" + "\n".join(f"{i}. {q.text}" for i, q in enumerate(qs, 1))
    full = f"{body}\n\n{text(lang, 'outro')}"
    pending = dict(pending, kinds=[q.kind for q in qs],
                   all={q.kind: next((o["value"] for o in q.options if o.get("all")), "") for q in qs},
                   options={q.kind: [o["value"] for o in q.options if not o.get("all")] for q in qs})
    # "options" (flat, with a ready-made question) keeps older clients and the CLI working.
    flat = [{"label": o["label"], "question": f"{pending['question']} ({o['value']})"}
            for q in qs if len(qs) == 1 for o in q.options]
    return {"text": full, "intro": text(lang, "intro_one" if len(qs) == 1 else "intro"),
            "outro": text(lang, "outro"), "questions": [q.to_dict() for q in qs], "options": flat,
            "entities": [o["value"] for q in qs if q.kind == "standard_version" for o in q.options if not o.get("all")],
            "dont_know": text(lang, "dont_know"), "pending": pending}


# ------------------------------------------------------------------------------- replies to a question
@dataclass
class Reply:
    question: str                 # the original question merged with the reply
    answered: list[str]           # condition kinds asked so far (never asked again in this thread)
    unknown: list[str]            # kinds the user does not know: answer conditionally
    round: int


def _clean(s: object, limit: int = 4000) -> str:
    return str(s or "").strip()[:limit]


def merge_reply(message: str, pending: Optional[dict]) -> Optional[Reply]:
    """The user's message merged into the pending question, or None when it is a new question."""
    if not isinstance(pending, dict) or not _clean(pending.get("question")):
        return None
    msg = _clean(message)
    original = _clean(pending.get("question"))
    kinds = [k for k in (pending.get("kinds") or []) if k in KINDS]
    answered = list(dict.fromkeys([k for k in (pending.get("answered") or []) if k in KINDS] + kinds))
    try:
        rnd = int(pending.get("round") or 1)
    except (TypeError, ValueError):
        rnd = 1
    opts = pending.get("options") if isinstance(pending.get("options"), dict) else {}
    values = [_clean(v, 200) for vs in opts.values() if isinstance(vs, list) for v in vs if _clean(v, 200)]
    all_ = pending.get("all") if isinstance(pending.get("all"), dict) else {}
    values += [_clean(v, 200) for v in all_.values() if _clean(v, 200)]
    if not msg:
        return None
    if _UNKNOWN.search(msg) and len(msg) <= 120:
        rest = _UNKNOWN.sub("", msg).strip(" ,.;:-")
        extra = []
        if "standard_version" in kinds and all_.get("standard_version") and not rest:
            extra.append(_clean(all_["standard_version"], 200))  # unknown version: compare the loaded ones
        if rest and len(rest.split()) <= 8 and "?" not in rest:
            extra.insert(0, rest)
        merged = f"{original} ({'; '.join(extra)})" if extra else original
        return Reply(merged, answered, kinds, rnd + 1)
    folded = fold_tr(msg)
    is_question = "?" in msg or bool(_QUESTION.search(msg))
    if is_question:
        return None  # a question of its own: a new topic, the pending clarification is dropped
    picked = any(fold_tr(v) in folded for v in values)
    if picked or (len(msg) <= 80 and len(msg.split()) <= 8):
        return Reply(f"{original} ({msg.rstrip('.')})", answered, [], rnd + 1)
    return None
