"""Replay recorded RRF exactly to attribute missing evidence, without tuning."""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from eval_retrieval_scale import ROOT, sha, write_json

sys.path.insert(0, str(ROOT / 'backend'))
from app.rag.retriever import _limit_by_source, _rrf_fuse
from app.rag.store import Hit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    report = json.loads((args.run_dir / 'report.json').read_text())
    source = 'backend/app/rag/retriever.py'
    if sha((ROOT / source).read_bytes()) != report['manifest']['sources'][source]:
        parser.error('retriever source changed since run; replay must use recorded revision')
    config = report['manifest']['config']
    counts = defaultdict(Counter)
    rows, replayed = [], 0
    for trial in report['trials']:
        raw = json.loads((args.run_dir / trial['per_question_file']).read_text())
        for row in raw:
            if row['method'] != 'hybrid':
                continue
            stages = row['diagnosis']['stages']

            def hits(name):
                return [Hit(id=h['chunk_id'], text='', score=h['score'],
                            metadata={'source_url': h['source_url']}) for h in stages[name]['hits']]

            vector, bm25 = hits('vector_candidates'), hits('bm25_candidates')
            fused = _rrf_fuse(vector, bm25, config['RRF_K'],
                              max(10, config['RRF_FUSION_CANDIDATE_N']),
                              config['RRF_VECTOR_WEIGHT'], config['RRF_BM25_WEIGHT'])
            limited = _limit_by_source(fused, 10, config['RAG_MAX_CHUNKS_PER_SOURCE'])
            if [h.id for h in limited] != [h['chunk_id'] for h in stages['rrf_candidates']['hits']]:
                raise ValueError(f"RRF replay mismatch: {trial['seed']} {row['question_id']}")
            if [h.id for h in limited[:5]] != row['retrieved_chunk_ids']:
                raise ValueError('final replay mismatch')
            replayed += 1
            if row['split'] != 'test':
                continue
            union_ids = {h.id for h in vector + bm25}
            fused_ids, limited_ids = {h.id for h in fused}, {h.id for h in limited}
            for gold in set(row['gold_chunk_ids']) - set(row['retrieved_chunk_ids']):
                if gold not in union_ids:
                    reason = 'absent_both_candidate_routes'
                elif gold not in fused_ids:
                    reason = 'raw_rrf_top10_truncation'
                elif gold not in limited_ids:
                    reason = 'per_source_limit_after_rrf'
                else:
                    reason = 'final_top5_truncation'
                counts[trial['documents']][reason] += 1
                rows.append({'documents': trial['documents'], 'seed': trial['seed'],
                             'question_id': row['question_id'], 'missing_gold': gold, 'reason': reason})
    write_json(args.run_dir / 'fusion-stage-attribution.json', {
        'replayed_hybrid_rows': replayed, 'replay_mismatches': 0,
        'missing_gold_labels_by_size': {str(k): dict(v) for k, v in counts.items()},
        'per_missing_label': rows,
        'scope': 'exact recorded-parameter replay; no alternative parameters tested; sequential loss attribution, not independent causal effect'})
    print(json.dumps({'replayed': replayed, 'missing_labels': {k: dict(v) for k, v in counts.items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
