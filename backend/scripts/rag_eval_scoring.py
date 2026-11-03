"""Deterministic checks for the case's annotated claims and prompt citations."""

import re

_CITATION = re.compile(r"\[证据([1-9][0-9]*)\]")
_CITATION_LIKE = re.compile(r"[\[［【]\s*证据[^\s\[［【\]］】。！？；]*[\]］】]?")
_SENTENCE_END = re.compile(r"[。！？；!?;\n]|\.(?=\s|$)")
_FLAGS = re.IGNORECASE
_REQUEST = re.compile(r"请(?:先)?(?:补充|提供|确认|核实|告知|说明|查看|问)|[？?]")


def _matches(pattern, text):
    return re.search(pattern, text, _FLAGS) is not None


def _assertions(answer):
    """Bind citations to one sentence or line, including post-punctuation citations."""
    parts = _SENTENCE_END.split(answer)
    previous = ""
    for part in parts:
        leading = re.match(r"\s*(?:\[证据[1-9][0-9]*\]\s*)+", part)
        if previous.strip() and leading:
            previous += leading[0]
            part = part[leading.end():]
        if previous.strip():
            yield previous
        previous = part or ""
    if previous.strip():
        yield previous


def score_answer(case, answer, injected):
    """Score a generated answer against the actual ordered prompt evidence.

    Case regexes define assertion polarity and accepted paraphrases. The same
    regex checks evidence text unless the fact specifies an evidence_pattern.
    """
    expected = case["expected"]
    action = expected["action"]
    if action not in {"answer", "clarify", "refuse"}:
        raise ValueError(f"Unknown expected action: {action}")
    if not isinstance(answer, str):
        raise TypeError("answer must be a string")

    documents = {str(index): segment for index, segment in enumerate(injected, 1)}
    injected_ids = {segment["document_id"] for segment in injected}
    citations = set(_CITATION.findall(answer))
    forbidden_docs = set(expected.get("forbidden_evidence", []))
    missing_required = sorted(set(expected.get("required_evidence", [])) - injected_ids)
    forbidden_injected = sorted(forbidden_docs & injected_ids)
    forbidden_cited = sorted(forbidden_docs & {
        documents[number]["document_id"] for number in citations if number in documents
    })
    malformed = [
        match[0] for match in _CITATION_LIKE.finditer(answer)
        if not _CITATION.fullmatch(match[0])
    ]
    unknown = sorted(citations - documents.keys(), key=lambda number: (len(number), number))
    forbidden_patterns = [
        pattern for pattern in expected.get("answer_excludes", []) if _matches(pattern, answer)
    ]

    assertions = [
        (_CITATION.sub("", sentence), set(_CITATION.findall(sentence)))
        for sentence in _assertions(answer)
    ]
    fact_results = []
    for fact in expected.get("facts", []):
        local = [
            numbers for assertion, numbers in assertions if _matches(fact["pattern"], assertion)
        ]
        supporting = {
            number for number, segment in documents.items()
            if segment["document_id"] in fact["evidence"]
            and _matches(fact.get("evidence_pattern", fact["pattern"]), segment["text"])
        }
        fact_results.append({
            "id": fact["id"],
            "present": bool(local),
            "supported": bool(local and supporting),
            "cited": bool(local) and all(numbers & supporting for numbers in local),
        })

    behavior_patterns = expected.get(f"{action}_patterns", [])
    if action == "refuse" and not behavior_patterns:
        behavior_patterns = [r"无法|不能|无权|权限不足|拒绝|无.{0,8}(?:资料|证据|权限)|没有.{0,8}(?:资料|证据|权限)|不足以|不支持"]
    behavior_text = answer
    if action == "clarify":
        sentences = re.split(r"(?<=[。！？!?；])|\n", answer)
        behavior_text = "\n".join(sentence for sentence in sentences if _REQUEST.search(sentence))
    behavior_passed = bool(behavior_text.strip()) and (
        all(_matches(pattern, behavior_text) for pattern in behavior_patterns)
        if behavior_patterns else action == "answer" and "知识库中暂无相关内容" not in answer
    )
    facts_total = len(fact_results)
    facts_present = sum(fact["present"] for fact in fact_results)
    facts_supported = sum(fact["supported"] for fact in fact_results)
    facts_cited = sum(fact["cited"] for fact in fact_results)
    passed = (
        behavior_passed
        and facts_present == facts_supported == facts_cited == facts_total
        and not (missing_required or forbidden_injected or forbidden_cited
                 or malformed or unknown or forbidden_patterns)
    )
    return {
        "passed": passed,
        "behavior_passed": behavior_passed,
        "facts_total": facts_total,
        "facts_present": facts_present,
        "facts_supported": facts_supported,
        "facts_cited": facts_cited,
        "required_evidence_present": not missing_required,
        "missing_required_evidence": missing_required,
        "forbidden_evidence_injected": forbidden_injected,
        "forbidden_evidence_cited": forbidden_cited,
        "forbidden_answer_patterns": forbidden_patterns,
        "malformed_citations": malformed,
        "unknown_citations": unknown,
        "fact_results": fact_results,
    }
