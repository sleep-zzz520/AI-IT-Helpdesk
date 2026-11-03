from scripts.rag_eval_scoring import score_answer


def _case():
    return {"expected": {
        "action": "answer",
        "required_evidence": ["vpn"],
        "forbidden_evidence": ["restricted"],
        "facts": [{
            "id": "backup",
            "pattern": r"先备份旧证书",
            "evidence": ["vpn"],
        }],
        "answer_excludes": [r"不(?:要|必).{0,5}备份旧证书"],
    }}


def _segment(document_id, text):
    return {"document_id": document_id, "text": text}


def test_citation_resolves_against_prompt_order():
    injected = [_segment("vpn-old", "先备份旧证书"), _segment("vpn", "先备份旧证书")]
    assert score_answer(_case(), "先备份旧证书。[证据2]", injected)["passed"]
    result = score_answer(_case(), "先备份旧证书[证据1]。", injected)
    assert result["facts_present"] == result["facts_supported"] == 1
    assert result["facts_cited"] == 0
    assert not result["passed"]


def test_correct_document_without_claim_in_injected_chunk_fails():
    injected = [_segment("vpn", "打开 VPN 客户端")]
    result = score_answer(_case(), "先备份旧证书[证据1]。", injected)
    assert result["facts_present"] == 1
    assert result["facts_supported"] == result["facts_cited"] == 0
    assert not result["passed"]


def test_citation_on_unrelated_sentence_does_not_support_claim():
    injected = [_segment("vpn", "先备份旧证书")]
    result = score_answer(_case(), "先备份旧证书。然后打开客户端[证据1]。", injected)
    assert result["facts_cited"] == 0


def test_negation_unknown_malformed_and_forbidden_evidence():
    injected = [_segment("vpn", "先备份旧证书"), _segment("restricted", "内部令牌")]
    result = score_answer(
        _case(), "不要先备份旧证书[证据1][证据2][证据3]［证据4］。", injected
    )
    assert result["forbidden_answer_patterns"]
    assert result["unknown_citations"] == ["3"]
    assert result["malformed_citations"] == ["［证据4］"]
    assert result["forbidden_evidence_injected"] == result["forbidden_evidence_cited"] == [
        "restricted"
    ]
    assert not result["passed"]


def test_clarification_and_refusal_patterns():
    clarify = {"expected": {"action": "clarify", "clarify_patterns": ["哪种系统", "版本"]}}
    refuse = {"expected": {"action": "refuse", "refuse_patterns": ["无权查看"]}}
    assert score_answer(clarify, "请问是哪种系统，版本是多少？", [])["passed"]
    assert not score_answer(clarify, "请问是哪种系统？", [])["passed"]
    assert score_answer(refuse, "当前账户无权查看该文档。", [])["passed"]
    assert not score_answer(refuse, "这里有访问令牌。", [])["passed"]
