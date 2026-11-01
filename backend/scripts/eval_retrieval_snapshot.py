"""显式临时 collection 上的当前60题真实检索快照，复用rag_eval三方法。

从backend运行；需要现成临时索引，不构建/同步语料。真实embedding批量一次，
10秒超时、SDK零重试、外层一次；禁用rerank，不触碰在岗库。
"""
import argparse
import json
import os
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vector-dir', type=Path, required=True)
    parser.add_argument('--collection', required=True)
    parser.add_argument('--output', type=Path, default=Path('tests/quality_reports/retrieval_current.json'))
    args = parser.parse_args()
    directory = args.vector_dir.resolve()
    # 必须显式指定临时数据目录；避免误把常用配置当评测目标。
    if not directory.is_dir() or not str(directory).startswith(('/private/tmp/', '/tmp/')):
        parser.error('评测只接受现成/tmp目录')
    os.environ['RERANK_ENABLED'] = 'false'
    os.environ['KB_VECTOR_DIR'] = str(directory)
    from app.config import settings
    from app.rag import embedder
    from app.rag.retriever import (
        RetrievalStats,
        _build_where,
        retrieve,
        retrieve_vector_only,
    )
    from app.rag.store import ChromaStore, list_collections
    from tests.rag_eval import _evaluate, _hybrid_diagnosis, _write_report
    from tests.rag_eval_dataset import RETRIEVAL_CASES, resolve_gold_chunk_ids
    settings.KB_VECTOR_DIR = str(directory)
    settings.RERANK_ENABLED = False
    if args.collection not in list_collections(str(directory)):
        parser.error('临时collection不存在；不会自动建立或同步')
    store = ChromaStore(str(directory), args.collection)
    gold = resolve_gold_chunk_ids(RETRIEVAL_CASES, Path(settings.KB_ROOT))
    client = embedder._client.with_options(timeout=10.0, max_retries=0)
    embedder._client = client
    embedder.MAX_RETRIES = 1
    start = time.perf_counter()
    try:
        vectors = embedder.embed_texts([case.query for case in RETRIEVAL_CASES])
    finally:
        client.close()
    vector_by_case = dict(zip((case.case_id for case in RETRIEVAL_CASES), vectors, strict=True))

    def raw(case):
        return store.query(vector_by_case[case.case_id], top_k=5, where=_build_where(None)), None

    def matched(case):
        return retrieve_vector_only(case.query, top_k=5, store=store,
                                    query_vector=vector_by_case[case.case_id]), None

    def hybrid(case):
        stats = RetrievalStats()
        hits = retrieve(case.query, top_k=5, store=store, stats=stats, debug=True,
                        query_vector=vector_by_case[case.case_id])
        return hits, _hybrid_diagnosis(stats, hits, gold[case.case_id])

    results = [_evaluate(name, fn, gold) for name, fn in [
        ('vector_only_raw', raw), ('vector_only_matched', matched), ('hybrid', hybrid)]]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write_report(args.output, results, collection=store.collection_name)
    report = json.loads(args.output.read_text())
    report['measurement'] = {
        'kind': 'current_real_embedding_snapshot', 'comparison': 'same current index and same query vectors; no before/after claim',
        'vector_dir': str(directory), 'chunks': store.count(), 'rerank_enabled': False,
        'embedding_model': embedder.EMBED_MODEL, 'embedding_query_count': len(vectors),
        'embedding_api_batch_count': 1, 'api_timeout_seconds': 10, 'sdk_max_retries': 0,
        'outer_attempts': 1, 'elapsed_seconds': round(time.perf_counter()-start, 3),
        'limits': ['60题人工标注合成运维问题；非真实用户流量', '只测检索与排序，不测生成与工具执行',
                   '本轮没有调整排序；不同方法比较不能当本轮修复的前后收益']}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'report': str(args.output), 'chunks': store.count(),
                      'results': [{k:v for k,v in item.items() if k != 'per_case'} for item in results]},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
