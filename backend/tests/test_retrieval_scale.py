"""Evaluation invariants: full evidence, strict chunk labels, honest failures."""
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'eval_retrieval_scale', Path(__file__).parents[1] / 'scripts/eval_retrieval_scale.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_multigold_recall_is_not_document_hit_or_mrr():
    got = module.metrics(['target:2', 'other:1'], ['target:2', 'target:4'])
    assert got == {'recall@1': .5, 'recall@3': .5, 'recall@5': .5,
                   'mrr': 1., 'all_gold@5': 0.}
    assert module.metrics(['target:2', 'target:4'], ['target:2', 'target:4'])['all_gold@5'] == 1.


def test_failed_query_stays_in_macro_denominator():
    base = {'family_id': 'x', 'split': 'test', 'question_type': 'direct',
            'method': 'hybrid', 'elapsed_ms': 1., 'parent_document_coverage': 0.}
    rows = [{**base, 'metrics': module.metrics(['a:1'], ['a:1']), 'error': None},
            {**base, 'metrics': module.metrics([], ['a:1']), 'error': 'RuntimeError'}]
    got = next(x for x in module.summarize(rows) if x['group'] == 'test')
    assert (got['attempted'], got['failed'], got['recall@5']) == (2, 1, .5)


def test_gold_must_resolve_to_exact_child_and_evidence():
    documents = [{'id': 'a', 'title': '测试', 'scenario': 'test', 'status': 'active',
                  'valid_to': '2099-12-31', 'body': '# 测试\n## 诊断\n证书已过期。\n## 操作\n执行续期。'}]
    compiled = module.compile_documents(documents)
    questions = [{'question_id': 'q', 'gold': [
        {'document_id': 'a', 'section': '诊断', 'evidence': '证书已过期。'},
        {'document_id': 'a', 'section': '操作', 'evidence': '执行续期。'}]}]
    assert module.resolve_gold(questions, compiled) == {'q': ['a:1', 'a:2']}
    questions[0]['gold'][1]['section_index'] = 1
    with pytest.raises(ValueError, match='incorrect section_index'):
        module.resolve_gold(questions, compiled)
    questions[0]['gold'][1]['section_index'] = 2
    questions[0]['gold'][1]['evidence'] = '不存在的动作'
    with pytest.raises(ValueError, match='unresolved'):
        module.resolve_gold(questions, compiled)


def test_p95_not_reported_for_tiny_latency_sample():
    assert module.distribution([1., 2., 3.])['p95'] is None
    assert module.distribution(list(range(240)))['p95'] is not None


def test_chroma_raw_oracle_compares_original_nonunit_vector(tmp_path):
    """Cosine search normalization is not the raw get(embeddings) contract."""
    from app.rag.chunks import Chunk
    from app.rag.store import ChromaStore
    vector = [.3, .4] + [0.] * 1022
    store = ChromaStore(str(tmp_path / 'chroma'), 'non-unit-oracle')
    store.upsert_chunks([Chunk('sample:1', '原始向量核验', {'doc_id': 'sample'})], [vector])
    got = store.get_vectors(['sample:1'])['sample:1']
    assert got == pytest.approx(vector, abs=2e-6)
    assert sum(x * x for x in got) == pytest.approx(.25, abs=2e-6)
