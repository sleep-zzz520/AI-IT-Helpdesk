"""Independent expiry/deletion/no-answer retrieval probes, outside Recall denominators."""
import argparse
import json
import os
import random
import sys
from dataclasses import replace
from pathlib import Path

from eval_retrieval_scale import EmbeddingCache, compile_documents, sha, write_json

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() or ROOT / '.rag' not in output.parents:
        parser.error('require new isolated .rag output directory')
    output.mkdir(parents=True)
    os.environ['KB_SYNC_MODE'] = 'legacy'
    os.environ['KB_VECTOR_DIR'] = str(output / 'chroma')
    os.environ['RERANK_ENABLED'] = 'false'
    from app.rag.bm25 import Bm25Index
    from app.rag.retriever import retrieve, retrieve_vector_only
    from app.rag.store import ChromaStore
    data = json.loads(args.dataset.read_text())
    compiled = compile_documents(data['documents'])
    targets = [d['id'] for d in data['documents'] if d['kind'] == 'target']
    distractors = sorted(d['id'] for d in data['documents'] if d['kind'] == 'distractor')
    random.Random(20261102).shuffle(distractors)
    chosen = targets + distractors[:64 - len(targets)]
    questions = [q for q in data['probes'] if q['question_type'] in ('expired', 'deleted')]
    no_answer = [q for q in data['probes'] if q['question_type'] == 'unanswerable']
    affected = [q['excluded_document_ids'][0] for q in questions]
    chosen += affected
    cache = EmbeddingCache(args.dataset.parent / 'embedding-cache', output / 'api-events.jsonl')
    chunks = [c for doc_id in chosen for c in compiled[doc_id].children]
    vectors = cache.get([c.text for c in chunks], 'probe-content')
    by_id = dict(zip([c.id for c in chunks], vectors, strict=True))
    queries = cache.get([q['query'] for q in questions + no_answer], 'probe-query')
    store = ChromaStore(str(output / 'chroma'), 'retrieval-filter-probes')

    def upsert(splits, make_active=False):
        batch, embeddings = [], []
        for split in splits:
            for chunk in [split.parent, *split.children]:
                if make_active:
                    chunk = replace(chunk, metadata={**chunk.metadata, 'valid_to': 20991231, 'status': 'active'})
                batch.append(chunk)
                embeddings.append([0.] * 1024 if chunk.metadata['chunk_type'] == 'parent' else by_id[chunk.id])
        for offset in range(0, len(batch), 256):
            store.upsert_chunks(batch[offset:offset + 256], embeddings[offset:offset + 256])

    upsert([compiled[d] for d in chosen], make_active=True)

    def search(query, vector, bm25):
        return {'vector_matched': [h.id for h in retrieve_vector_only(query, store=store, query_vector=vector)],
                'hybrid': [h.id for h in retrieve(query, store=store, bm25=bm25, query_vector=vector, joint=False)]}

    baseline_bm25 = Bm25Index(store)
    before = [search(q['query'], v, baseline_bm25) for q, v in zip(questions, queries[:8], strict=True)]
    # Restore the frozen expired/deleted metadata, then test before physical deletion.
    upsert([compiled[d] for d in affected])
    after_bm25 = Bm25Index(store)
    rows = []
    for index, (doc_id, question, vector) in enumerate(zip(affected, questions, queries[:len(questions)], strict=True)):
        current = search(question['query'], vector, after_bm25)
        forbidden = {compiled[doc_id].parent.id, *[c.id for c in compiled[doc_id].children]}
        baseline_exposed_by_method = {method: bool(forbidden & set(hits))
                                      for method, hits in before[index].items()}
        baseline_exposed = all(baseline_exposed_by_method.values())
        leaked = {method: sorted(forbidden & set(hits)) for method, hits in current.items()}
        rows.append({'kind': question['question_type'], 'document_id': doc_id,
                     'question_id': question['question_id'], 'query': question['query'],
                     'baseline_hits': before[index], 'after_hits': current, 'baseline_exposed': baseline_exposed,
                     'baseline_exposed_by_method': baseline_exposed_by_method,
                     'pass_by_method': {method: not leaked[method]
                                        if exposed else None
                                        for method, exposed in baseline_exposed_by_method.items()},
                     'leaked_ids': leaked,
                     'pass': baseline_exposed and not any(leaked.values())})
    for row in rows:
        if row['kind'] == 'deleted':
            doc_id = row['document_id']
            forbidden = {compiled[doc_id].parent.id, *[c.id for c in compiled[doc_id].children]}
            store.delete_by_doc_id(doc_id)
            row['physically_absent'] = not store.get_by_ids(list(forbidden))
            row['pass'] = row['pass'] and row['physically_absent']
    after_bm25 = Bm25Index(store)
    for question, vector in zip(no_answer, queries[len(questions):], strict=True):
        current = search(question['query'], vector, after_bm25)
        rows.append({'kind': 'no_answer', 'query': question['query'],
                     'question_id': question['question_id'], 'conditions': question['conditions'],
                     'after_hits': current, 'nonempty': {method: bool(hits) for method, hits in current.items()},
                     'interpretation': 'diagnostic only; retrieval candidates do not prove generated refusal or hallucination'})
    report = {'documents_initial': len(chosen), 'document_ids': chosen, 'probes': rows,
              'dataset_sha256': sha(args.dataset.read_bytes()), 'runner_sha256': sha(Path(__file__).read_bytes()),
              'filter_probes_attempted': 8, 'filter_probes_passed': sum(r.get('pass', False) for r in rows),
              'no_answer_probes': 4,
              'limits': ['isolated legacy Chroma read path; not Git generation deny/rollback safety validation',
                         'BM25 rebuilt after mutations; cache invalidation race not measured',
                         'expiry retains raw stored content but filters ranked evidence; deletion removes children and parents',
                         'no-answer unsupported applicability scopes; small diagnostic set, no refusal accuracy score'],
              'new_embedding_requests': cache.requests}
    write_json(output / 'report.json', report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('probes', 'new_embedding_requests')}, ensure_ascii=False))
    cache.client.close()


if __name__ == '__main__':
    main()
