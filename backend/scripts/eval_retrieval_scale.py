"""Frozen, paired semantic retrieval benchmark. No production DB/sync writes.

Run from repository root with .venv/bin/python backend/scripts/eval_retrieval_scale.py
--dataset ... --output-dir ... . A completed output directory is never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def metrics(ids, gold):
    if not gold:
        raise ValueError('answerable case requires nonempty gold')
    gold = set(gold)
    first = next((i for i, cid in enumerate(ids[:5], 1) if cid in gold), None)
    return {**{f'recall@{k}': len(set(ids[:k]) & gold) / len(gold) for k in (1, 3, 5)},
            'mrr': 1 / first if first else 0.,
            'all_gold@5': float(gold <= set(ids[:5]))}


def distribution(values):
    if not values:
        return {'n': 0}
    return {'n': len(values), 'median': statistics.median(values),
            'p95': float(np.percentile(values, 95)) if len(values) >= 200 else None,
            'min': min(values), 'max': max(values)}


class EmbeddingCache:
    """Only real provider responses populate this content-addressed cache."""

    def __init__(self, directory, events):
        from app.rag import embedder
        self.module = embedder
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.events = events
        self.client = embedder._client.with_options(timeout=30., max_retries=0)
        self.requests = []

    def key(self, text):
        identity = [self.module.EMBED_MODEL, self.module.EMBED_DIM,
                    str(self.client.base_url), self.module._clean_text(text)]
        return sha(json.dumps(identity, ensure_ascii=False).encode())

    def get(self, texts, kind):
        keys = [self.key(t) for t in texts]
        missing = {}
        for key, text in zip(keys, texts, strict=True):
            if not (self.directory / f'{key}.npy').exists():
                missing[key] = self.module._clean_text(text)
        pending = list(missing.items())
        for offset in range(0, len(pending), self.module.BATCH_SIZE):
            batch = pending[offset:offset + self.module.BATCH_SIZE]
            if any(not t.strip() for _, t in batch):
                raise ValueError('empty embedding input')
            start = time.perf_counter()
            event = {'kind': kind, 'keys': [k for k, _ in batch], 'inputs': len(batch),
                     'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'ok': False}
            try:
                response = self.client.embeddings.create(
                    model=self.module.EMBED_MODEL, dimensions=self.module.EMBED_DIM,
                    input=[t for _, t in batch])
                indexed = {item.index: item.embedding for item in response.data}
                if len(response.data) != len(batch) or set(indexed) != set(range(len(batch))):
                    raise ValueError('provider returned incomplete or duplicate indices')
                vectors = [np.asarray(indexed[i], dtype=np.float32) for i in range(len(batch))]
                if any(v.shape != (self.module.EMBED_DIM,) or not np.isfinite(v).all()
                       or np.linalg.norm(v) == 0 for v in vectors):
                    raise ValueError('invalid provider embedding')
                for (key, _), vector in zip(batch, vectors, strict=True):
                    np.save(self.directory / f'{key}.npy', vector)
                event['ok'] = True
                event['usage'] = response.usage.model_dump() if response.usage else None
            except Exception as exc:
                event['error_type'] = type(exc).__name__
                event['http_status'] = getattr(exc, 'status_code', None)
                raise RuntimeError('real embedding failed; retained request event; no silent substitution') from None
            finally:
                event['elapsed_ms'] = (time.perf_counter() - start) * 1000
                self.requests.append(event)
                with self.events.open('a') as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + '\n')
                print(json.dumps({k: v for k, v in event.items() if k != 'keys'}), flush=True)
        result = [np.load(self.directory / f'{k}.npy', allow_pickle=False).tolist() for k in keys]
        if any(len(v) != self.module.EMBED_DIM or not np.isfinite(v).all() for v in result):
            raise ValueError('corrupt cached embedding')
        return result


def compile_documents(documents):
    from app.rag.chunks import KnowledgeDoc
    from app.rag.splitter import split_markdown
    compiled = {}
    for doc in documents:
        meta = {key: doc[key] for key in ('title', 'scenario', 'status', 'valid_to')}
        meta['source_url'] = f"synthetic/{doc['id']}.md"
        result = split_markdown(KnowledgeDoc(meta['source_url'], doc['id'], meta, doc['body']))
        compiled[doc['id']] = result
    return compiled


def resolve_gold(questions, compiled):
    resolved = {}
    for question in questions:
        ids = []
        for label in question['gold']:
            children = compiled[label['document_id']].children
            matches = [c for c in children if c.text.splitlines()[0] == f"## {label['section']}"
                       and label['evidence'] in c.text]
            if len(matches) != 1:
                raise ValueError(f"unresolved/ambiguous gold {question['question_id']}")
            sections = [c for c in children if c.text.startswith('## ')]
            if 'section_index' in label and sections.index(matches[0]) + 1 != label['section_index']:
                raise ValueError(f"incorrect section_index {question['question_id']}")
            ids.append(matches[0].id)
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate gold labels')
        resolved[question['question_id']] = ids
    return resolved


def run_collection(store, questions, gold, query_vectors, seed):
    from app.config import settings
    from app.rag.bm25 import Bm25Index
    from app.rag.retriever import (
        RetrievalStats,
        _attach_parents,
        _enhance_query,
        _limit_by_source,
        _recall_store,
        retrieve,
        retrieve_vector_only,
    )
    from tests.rag_eval import _hybrid_diagnosis
    bm25 = Bm25Index(store)
    start = time.perf_counter()
    bm25._ensure_built()
    build_ms = (time.perf_counter() - start) * 1000
    rows = []
    for i, case in enumerate(questions):
        qid = case['question_id']
        methods = ['vector_matched', 'hybrid', 'bm25_only']
        methods = methods[(i + seed) % 3:] + methods[:(i + seed) % 3]
        for method in methods:
            stats = RetrievalStats()
            start = time.perf_counter()
            error = None
            try:
                if method == 'vector_matched':
                    hits = retrieve_vector_only(case['query'], store=store, top_k=5,
                        query_vector=query_vectors[qid], stats=stats, debug=True)
                elif method == 'hybrid':
                    hits = retrieve(case['query'], store=store, bm25=bm25, top_k=5,
                        query_vector=query_vectors[qid], stats=stats, debug=True, joint=False)
                else:
                    _, raw = _recall_store(store, bm25, _enhance_query(case['query']), None, None)
                    hits = _limit_by_source(raw, 5, max(1, settings.RAG_MAX_CHUNKS_PER_SOURCE))
                    _attach_parents(hits, store)
            except Exception as exc:
                hits, error = [], type(exc).__name__
            elapsed = (time.perf_counter() - start) * 1000
            ids = [h.id for h in hits]
            parent_ids = {h.metadata.get('doc_id') for h in hits if h.parent_text}
            item = {'question_id': qid, 'family_id': case['family_id'], 'split': case['split'],
                    'question_type': case['question_type'], 'method': method,
                    'gold_chunk_ids': gold[qid], 'retrieved_chunk_ids': ids,
                    'metrics': metrics(ids, gold[qid]), 'elapsed_ms': elapsed, 'error': error,
                    'parent_document_coverage': len({g.rsplit(':', 1)[0] for g in gold[qid]} & parent_ids)
                        / len({g.rsplit(':', 1)[0] for g in gold[qid]}),
                    'hits': [{'id': h.id, 'score': h.score, 'source_url': h.metadata.get('source_url')}
                             for h in hits]}
            if method == 'hybrid':
                item['diagnosis'] = _hybrid_diagnosis(stats, hits, frozenset(gold[qid]))
                item['diagnosis']['any_gold_stage'] = item['diagnosis'].pop('failure_stage')
                item['diagnosis']['all_gold_final'] = set(gold[qid]) <= set(ids)
            rows.append(item)
    return rows, build_ms


def summarize(rows):
    groups = {}
    for row in rows:
        for group in (row['split'], f"{row['split']}:{row['question_type']}"):
            groups.setdefault((group, row['method']), []).append(row)
    result = []
    for (group, method), items in sorted(groups.items()):
        result.append({'group': group, 'method': method, 'attempted': len(items),
            'failed': sum(x['error'] is not None for x in items),
            **{key: statistics.mean(x['metrics'][key] for x in items) for key in items[0]['metrics']},
            'parent_document_coverage': statistics.mean(x['parent_document_coverage'] for x in items),
            'warm_retrieval_ms': distribution([x['elapsed_ms'] for x in items])})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--seeds', nargs='+', type=int, default=[20261102, 20261103, 20261104])
    parser.add_argument('--sizes', nargs='+', type=int, default=[64, 256, 512])
    parser.add_argument('--freeze-sha256', required=True)
    args = parser.parse_args()
    raw = args.dataset.read_bytes()
    if sha(raw) != args.freeze_sha256:
        parser.error('frozen dataset hash mismatch')
    output = args.output_dir.resolve()
    if output.exists():
        parser.error('output directory already exists; choose a new run directory')
    if ROOT / '.rag' not in output.parents:
        parser.error('output must be isolated under repository .rag/')
    output.mkdir(parents=True)
    # Set before importing app code: no SQL source, active pointer or live index resolution.
    os.environ['KB_SYNC_MODE'] = 'legacy'
    os.environ['KB_VECTOR_DIR'] = str(output / 'chroma')
    os.environ['RERANK_ENABLED'] = 'false'
    from app.config import settings
    from app.rag.store import ChromaStore
    settings.KB_SYNC_MODE = 'legacy'
    settings.KB_VECTOR_DIR = str(output / 'chroma')
    settings.RERANK_ENABLED = False
    data = json.loads(raw)
    write_json(output / 'dataset-frozen.json', data)
    compiled = compile_documents(data['documents'])
    gold = resolve_gold(data['questions'], compiled)
    manifest = {'dataset_sha256': sha(raw), 'source_commit': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'git_status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True),
        'python': sys.version, 'platform': platform.platform(),
        'package_versions': {name: importlib.metadata.version(name) for name in
                             ('chromadb', 'openai', 'numpy', 'jieba', 'rank-bm25')},
        'config': {key: getattr(settings, key) for key in ('VECTOR_MIN_SCORE', 'RRF_RECALL_N',
            'RRF_K', 'RRF_VECTOR_WEIGHT', 'RRF_BM25_WEIGHT', 'RRF_FUSION_CANDIDATE_N',
            'RAG_MAX_CHUNKS_PER_SOURCE', 'RERANK_ENABLED')},
        'seeds': args.seeds, 'sizes': args.sizes,
        'retrieval_scope': 'global; scenario=None; no gold-derived metadata filters',
        'sources': {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in [
            Path(__file__), ROOT / 'backend/scripts/build_retrieval_benchmark.py',
            *[ROOT / f'backend/app/rag/{name}.py' for name in
              ('retriever', 'store', 'splitter', 'bm25', 'embedder')]]},
        'documents': [{'id': d['id'], 'kind': d['kind'], 'body_sha256': sha(d['body'].encode()),
                       'child_chunks': len(compiled[d['id']].children)} for d in data['documents']]}
    for relative in manifest['sources']:
        target = output / 'source-snapshot' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    manifest['frozen_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    manifest['argv'] = sys.argv
    write_json(output / 'manifest.json', manifest)
    cache = EmbeddingCache(args.dataset.parent / 'embedding-cache', output / 'api-events.jsonl')
    children = [c for result in compiled.values() for c in result.children]
    vectors = cache.get([c.text for c in children], 'content')
    content_vectors = {c.id: v for c, v in zip(children, vectors, strict=True)}
    queries = cache.get([q['query'] for q in data['questions']], 'query')
    query_vectors = dict(zip([q['question_id'] for q in data['questions']], queries, strict=True))
    embedding_inputs = [
        {'kind': 'content', 'id': c.id, 'input_sha256': sha(c.text.encode()), 'cache_key': cache.key(c.text)}
        for c in children]
    embedding_inputs.extend({'kind': 'query', 'id': q['question_id'],
        'input_sha256': sha(q['query'].encode()), 'cache_key': cache.key(q['query'])} for q in data['questions'])
    for item in embedding_inputs:
        item['vector_file_sha256'] = sha((cache.directory / f"{item['cache_key']}.npy").read_bytes())
    write_json(output / 'embedding-input-manifest.json', embedding_inputs)
    targets = [d['id'] for d in data['documents'] if d['kind'] == 'target']
    pool = sorted(d['id'] for d in data['documents'] if d['kind'] == 'distractor')
    report = {'protocol': 'retrieval-scale-v1', 'manifest': manifest, 'trials': [],
              'embedding': {'model': cache.module.EMBED_MODEL, 'dimensions': cache.module.EMBED_DIM,
                            'query_inputs': len(queries), 'content_inputs': len(children),
                            'new_requests': cache.requests},
              'limitations': ['synthetic LLM-authored and LLM-audited; no human expert gold',
                  'same target questions across seeds; trials are not independent questions',
                  'warm local retrieval; OS page cache uncontrolled; no production SLO',
                  'strict child evidence recall is not answer accuracy; parent coverage separate']}
    for seed in args.seeds:
        shuffled = pool.copy()
        random.Random(seed).shuffle(shuffled)
        for size in args.sizes:
            chosen = targets + shuffled[:size - len(targets)]
            if len(chosen) != size:
                raise ValueError('invalid corpus size')
            store = ChromaStore(str(output / 'chroma'), f'semantic-n{size}-s{seed}')
            chunks, vectors = [], []
            for doc_id in chosen:
                split = compiled[doc_id]
                chunks.extend([split.parent, *split.children])
                vectors.extend([[0.] * cache.module.EMBED_DIM,
                                *[content_vectors[c.id] for c in split.children]])
            start = time.perf_counter()
            for offset in range(0, len(chunks), 256):
                store.upsert_chunks(chunks[offset:offset + 256], vectors[offset:offset + 256])
            build_ms = (time.perf_counter() - start) * 1000
            if store.count() != len(chunks):
                raise ValueError('index count mismatch')
            actual = store.get_all_meta()
            if set(actual) != {c.id for c in chunks} or any(
                    actual[c.id]['text'] != c.text or actual[c.id]['metadata'] != c.metadata
                    for c in chunks):
                raise ValueError('independent raw index text/metadata mismatch')
            stored_vectors = store.get_vectors([c.id for c in chunks])
            if any(not np.allclose(stored_vectors[c.id], expected, atol=2e-6, rtol=2e-6)
                   for c, expected in zip(chunks, vectors, strict=True)):
                raise ValueError('independent raw vector comparison failed')
            rows, bm25_ms = run_collection(store, data['questions'], gold, query_vectors, seed)
            name = f'n{size}-s{seed}'
            write_json(output / f'{name}-per-question.json', rows)
            trial = {'documents': size, 'seed': seed, 'document_ids': chosen, 'total_chunks': len(chunks),
                     'children': sum(len(compiled[d].children) for d in chosen),
                     'index_write_ms': build_ms, 'bm25_cold_build_ms': bm25_ms,
                     'raw_index_validation': 'all IDs/text/metadata and original stored real vectors passed',
                     'per_question_file': f'{name}-per-question.json', 'summary': summarize(rows),
                     'hybrid_any_gold_stages': dict(Counter(r['diagnosis']['any_gold_stage']
                        for r in rows if r['method'] == 'hybrid' and r['split'] == 'test'))}
            report['trials'].append(trial)
            write_json(output / 'report-partial.json', report)
            print(json.dumps({k: v for k, v in trial.items() if k not in ('summary', 'document_ids')},
                             ensure_ascii=False), flush=True)
    write_json(output / 'report.json', report)
    cache.client.close()


if __name__ == '__main__':
    main()
