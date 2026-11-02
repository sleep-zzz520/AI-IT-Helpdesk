"""Re-run the legacy 60 questions in a separate real-embedding isolated index."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'backend'), str(Path(__file__).parent)]

from eval_retrieval_scale import (
    EmbeddingCache,
    run_collection,
    sha,
    summarize,
    write_json,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cache-root', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() or ROOT / '.rag' not in output.parents:
        parser.error('require new isolated .rag output directory')
    output.mkdir(parents=True)
    os.environ['KB_SYNC_MODE'] = 'legacy'
    os.environ['KB_VECTOR_DIR'] = str(output / 'chroma')
    os.environ['RERANK_ENABLED'] = 'false'
    from app.rag.loader import load_doc
    from app.rag.splitter import split_markdown
    from app.rag.store import ChromaStore
    from tests.rag_eval_dataset import RETRIEVAL_CASES, resolve_gold_chunk_ids
    documents, chunks, source_manifest = [], [], []
    kb_root = ROOT / 'docs/knowledge'
    for path in sorted(kb_root.rglob('*.md')):
        doc = load_doc(path, kb_root)
        doc.meta['source_url'] = str(path.relative_to(ROOT))
        split = split_markdown(doc)
        documents.append(split)
        chunks.extend(split.children)
        source_manifest.append({'path': str(path.relative_to(ROOT)), 'sha256': sha(path.read_bytes())})
    cache = EmbeddingCache(args.cache_root, output / 'api-events.jsonl')
    vectors = cache.get([c.text for c in chunks], 'regression-content')
    by_id = dict(zip([c.id for c in chunks], vectors, strict=True))
    queries = cache.get([q.query for q in RETRIEVAL_CASES], 'regression-query')
    query_vectors = dict(zip([q.case_id for q in RETRIEVAL_CASES], queries, strict=True))
    gold = resolve_gold_chunk_ids(RETRIEVAL_CASES, kb_root)
    gold = {key: sorted(value) for key, value in gold.items()}
    store = ChromaStore(str(output / 'chroma'), 'legacy-regression-isolated')
    all_chunks, all_vectors = [], []
    for split in documents:
        all_chunks.extend([split.parent, *split.children])
        all_vectors.extend([[0.] * 1024, *[by_id[c.id] for c in split.children]])
    store.upsert_chunks(all_chunks, all_vectors)
    cases = [{'question_id': q.case_id, 'family_id': q.gold_chunks[0].source_url,
              'query': q.query, 'split': 'regression', 'question_type': q.question_type}
             for q in RETRIEVAL_CASES]
    rows, bm25_ms = run_collection(store, cases, gold, query_vectors, 20261102)
    write_json(output / 'per-question.json', rows)
    report = {'kind': 'legacy60-regression-only', 'measured_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'documents': len(documents), 'total_chunks': store.count(), 'questions': len(cases),
              'source_manifest': source_manifest, 'bm25_build_ms': bm25_ms,
              'summary': summarize(rows), 'new_embedding_requests': cache.requests,
              'limits': ['same previously tuned 60 questions; not independent new evidence',
                         'legacy first-gold labeling retained for historical comparability']}
    write_json(output / 'report.json', report)
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    cache.client.close()


if __name__ == '__main__':
    main()
