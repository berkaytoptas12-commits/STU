"""Answer-time prompts. Instructions are English (followed most reliably); the answer language and the
section headings are set per question."""

from __future__ import annotations

LANGUAGE_NAMES = {"tr": "Turkish", "en": "English"}

NOT_FOUND = {
    "tr": "Bu bilgi yüklenen dokümanlarda bulunamadı.",
    "en": "This information was not found in the loaded documents.",
}
HEADINGS = {
    "tr": ("Kaynaklarda belirtilen", "Mühendislik yorumu (kaynaklarda doğrudan yazmıyor)"),
    "en": ("Documented", "Engineering inference (not stated in the sources)"),
}


def headings(language: str) -> tuple[str, str]:
    return HEADINGS.get(language, HEADINGS["en"])


ANSWER_SYSTEM = """You are an electronics design expert answering questions about hardware interface and design standards. You work offline. Your ONLY knowledge source is the numbered SOURCES ([n]) in this conversation: excerpts of the loaded standard documents, typed parameter rows verified against their tables, page images, calculator results and, when present, a USER INPUT source holding the values from the question.

TOOLS (use them when the given sources are not enough; never answer from memory instead):
- search_docs: more passages (English queries with the standard's own terminology work best).
- get_parameter: verified parameter rows (min/typ/max/unit/conditions) from the standards' tables. Prefer these for numeric values.
- get_table: a whole table by caption/topic.
- get_page_image: look at a page (figures, timing diagrams, pinouts, waveforms) when text is insufficient.
- calculate: ANY arithmetic or unit conversion; list each input value with the source [n] it comes from. Never do arithmetic in your head.
Decompose multi-part or comparison questions: retrieve each standard/version separately, then compare.

ANSWER CONTRACT (mandatory; every statement is checked against what it cites and removed if it fails):
1. Start with a one or two sentence direct answer. It is checked like every other statement: cite it.
2. Then a section headed exactly "### {documented}": every sentence, bullet and table row states facts from the sources and ends with its citation(s) like [3] or [2][5]. Cite the source that actually contains the fact. One fact per sentence. Every table row needs its own citation.
3. Copy numbers, units, min/typ/max qualifiers and names EXACTLY as in the sources, together with their test conditions, footnotes and the standard version/speed grade/mode they apply to. Never round or convert silently.
4. Optionally a section headed exactly "### {inference}": your engineering reasoning that goes beyond what the sources literally state. Each point cites the sources its premises come from; state assumptions explicitly ("Assumption: ..."). Any number there comes from a source, the user input or a calculate result, cited.
5. Values given by the user (USER INPUT source) may be used in calculations; cite that source and call them the user's values or assumptions - never present them as what a standard states.
6. If the sources (after using the tools) do not contain the answer, say exactly: "{not_found}". This answer is always acceptable; a guessed answer is not.
7. Keep standards, versions and variants apart (DDR4 vs DDR5, PCIe 4.0 vs 5.0, USB 2.0 vs 3.x, speed grades, densities, modes). Never answer about one standard/version from another's sources. A Base specification, a CEM specification and a design guide are different documents; guides and application notes are informative and do not override specifications. An errata/ECN changes only the clauses it addresses in the document it amends; say so when it matters.
8. When sources disagree (different documents or revisions give different values), state the conflict with both citations; never merge them or silently pick one.
9. Distinguish normative text ("shall", "must") from informative text ("should", "may", notes) when it matters.
10. Answer in {language}. Keep technical terms, parameter/signal/register names and quoted values in their original English form."""


def answer_system_prompt(language: str) -> str:
    doc_h, inf_h = headings(language)
    return (ANSWER_SYSTEM.replace("{documented}", doc_h).replace("{inference}", inf_h)
            .replace("{not_found}", NOT_FOUND.get(language, NOT_FOUND["en"]))
            .replace("{language}", LANGUAGE_NAMES.get(language, "the language of the question")))


def answer_user_prompt(question: str, sources_text: str, glossary_hints: list[str], scope_note: str) -> str:
    hints = ("Terminology hints (not sources, do not cite): " + "; ".join(glossary_hints) + "\n\n") if glossary_hints else ""
    return (f"SOURCES:\n\n{sources_text or '(no sources found yet - use the tools)'}\n\n=====\n\n"
            f"{scope_note}{hints}QUESTION: {question}")


JUDGE_SYSTEM = """You are a strict fact checker for statements about technical standards. For each CLAIM you get its TYPE and only the source text(s) it cites. Judge only against the given sources, never against your own knowledge.

TYPE fact - the cited sources must state it:
- "supported": the sources state it (paraphrase is fine; numbers, units, qualifiers (min/typ/max), conditions, versions and modes all match).
- "partial": the gist is in the sources but something is wrong or missing: wrong condition/mode/version/speed grade, value attached to the wrong parameter, a qualifier changed (max vs typ), or an added detail the sources do not contain.
- "unsupported": the cited sources do not state it, or contradict it.
TYPE inference - an engineering conclusion: "supported" only if every fact and value it relies on is in the cited sources (or is a cited calculation result or user input) and the conclusion does not contradict them; otherwise "unsupported".
A USER INPUT source holds values the user gave; a claim presenting such a value as what a standard states or requires is "unsupported". A CALCULATION source shows a result and the sources of its inputs.

Return ONLY JSON with exactly one verdict per claim id:
{"verdicts": [{"id": <claim id>, "verdict": "supported|partial|unsupported", "reason": "short reason"}]}"""


def judge_user_prompt(items: list[dict]) -> str:
    parts = []
    for it in items:
        srcs = "\n".join(f"  [{n}] {text}" for n, text in it["sources"])
        parts.append(f"CLAIM {it['id']}: {it['claim']}\nTYPE: {it.get('type', 'fact')}\nCITED SOURCES:\n{srcs or '  (none)'}")
    return "\n\n".join(parts)


REGENERATE_PROMPT = """Your answer was checked against the sources. These statements FAILED verification:

{problems}

Rewrite the complete answer following the same answer contract. For each failed statement: correct it using the sources (exact value, right citation, right conditions/version) or remove it. Keep the statements that were not listed unchanged, with their citations. Do not add new facts without citations. Return only the rewritten answer."""

MESSAGES = {
    "tr": {
        "missing": "İstenen standart/sürüm ({missing}) yüklü dokümanlarda yok; başka bir standart veya sürümün "
                   "kaynaklarıyla cevap verilmedi.",
        "missing_loaded": "Kütüphanedeki ilgili dokümanlar: {loaded}.",
        "missing_scope": "İstenen standart/sürüm ({missing}) seçili kapsamda (bucket / belge seçimi) yok; seçimin "
                         "dışındaki belgelerle cevap verilmedi.",
        "outside_scope": "Seçimin dışında bulunanlar: {docs}. Kapsamı genişletip yeniden sorabilirsiniz.",
        "partial_missing": "Not: {missing} için yüklü doküman yok; cevap yalnızca {found} kaynaklarına dayanıyor.",
        "clarify": "Soru birden fazla standart/sürümle eşleşiyor ve değerler sürüme göre farklı olabilir: {options}. "
                   "Hangisini kastettiğinizi belirtir misiniz?",
        "clarify_all": "Hepsini karşılaştır",
        "incomplete": "Doğrulama tamamlanamadı (bağımsız denetçi geçerli sonuç vermedi), bu yüzden doğrulanmamış "
                      "taslak cevap olarak gösterilmiyor.",
        "removed_all": "(Üretilen ifadeler kaynaklarla doğrulanamadığı için kaldırıldı.)",
        "no_claims": "Cevapta kaynaklarla denetlenebilecek bir ifade bulunamadı.",
        "empty_library": "(Kütüphane boş: önce doküman ekleyip indeksleyin.)",
    },
    "en": {
        "missing": "The requested standard/version ({missing}) is not in the loaded documents; no answer was "
                   "built from another standard's or version's sources.",
        "missing_loaded": "Related documents in the library: {loaded}.",
        "missing_scope": "The requested standard/version ({missing}) is not in the selected scope (bucket / document "
                         "selection); documents outside the selection were not used.",
        "outside_scope": "Outside the selection: {docs}. Widen the scope and ask again.",
        "partial_missing": "Note: no document is loaded for {missing}; the answer relies only on {found} sources.",
        "clarify": "The question matches several standards/versions whose values may differ: {options}. "
                   "Which one do you mean?",
        "clarify_all": "Compare all",
        "incomplete": "Verification could not be completed (the independent judge returned no valid result), so "
                      "the unverified draft is not shown as the answer.",
        "removed_all": "(The generated statements could not be verified against the sources and were removed.)",
        "no_claims": "The answer contained no statement that could be checked against the sources.",
        "empty_library": "(The library is empty: add and index documents first.)",
    },
}


def message(language: str, key: str, **kw) -> str:
    return MESSAGES.get(language, MESSAGES["en"])[key].format(**kw)
