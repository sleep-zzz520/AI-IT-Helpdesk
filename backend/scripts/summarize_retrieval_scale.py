"""Summarize paired outcomes without treating repeated seeds as new questions."""
import argparse
import json
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from eval_retrieval_scale import distribution, write_json


def paired_interval(rows, metric, seed=7301):
    """First average seeds per question; resample complete question families."""
    by_question = defaultdict(lambda: defaultdict(list))
    families = {}
    for row in rows:
        by_question[row['question_id']][row['method']].append(row['metrics'][metric])
        families[row['question_id']] = row['family_id']
    by_family = defaultdict(list)
    for qid, methods in by_question.items():
        by_family[families[qid]].append(statistics.mean(methods['hybrid'])
                                       - statistics.mean(methods['vector_matched']))
    clusters = list(by_family.values())
    rng = random.Random(seed)
    boot = sorted(statistics.mean(value for group in rng.choices(clusters, k=len(clusters))
                                  for value in group) for _ in range(4000))
    return {'delta_hybrid_minus_vector': statistics.mean(value for group in clusters for value in group),
            'family_cluster_bootstrap_95pct': [boot[99], boot[3899]],
            'families': len(clusters), 'unique_questions': len(by_question),
            'resamples': 4000, 'seed': seed, 'scope': 'synthetic-family uncertainty; not real-user confidence'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    report = json.loads((args.run_dir / 'report.json').read_text())
    all_rows, paired, failures, trials = [], [], [], []
    question_pairs = {}
    for trial in report['trials']:
        rows = json.loads((args.run_dir / trial['per_question_file']).read_text())
        for row in rows:
            row['documents'], row['seed'] = trial['documents'], trial['seed']
        all_rows.extend(rows)
        test = [r for r in rows if r['split'] == 'test']
        by_case = defaultdict(dict)
        for row in test:
            by_case[row['question_id']][row['method']] = row
        for qid, methods in by_case.items():
            h, v = methods['hybrid'], methods['vector_matched']
            item = {'documents': trial['documents'], 'seed': trial['seed'], 'question_id': qid,
                    'family_id': h['family_id'], 'question_type': h['question_type'],
                    'recall@1_delta': h['metrics']['recall@1'] - v['metrics']['recall@1'],
                    'recall@5_delta': h['metrics']['recall@5'] - v['metrics']['recall@5'],
                    'mrr_delta': h['metrics']['mrr'] - v['metrics']['mrr']}
            paired.append(item)
            question_pairs[(trial['documents'], trial['seed'], qid)] = methods
            if h['metrics']['recall@5'] < 1:
                gold = set(h['gold_chunk_ids'])
                stages = h['diagnosis']['stages']
                available = {s: gold & {hit['chunk_id'] for hit in detail['hits']}
                             for s, detail in stages.items()}
                missing = gold - set(h['retrieved_chunk_ids'])
                never_candidates = missing - (available['vector_candidates'] | available['bm25_candidates'])
                failure = {**item, 'gold': sorted(gold), 'missing_gold': sorted(missing),
                           'missing_both_routes': sorted(never_candidates),
                           'present_candidates_but_missing_final': sorted(missing - never_candidates),
                           'hybrid_hits': h['retrieved_chunk_ids'],
                           'parent_document_coverage': h['parent_document_coverage'],
                           'error': h['error']}
                failures.append(failure)
        trials.append({'documents': trial['documents'], 'seed': trial['seed'],
                       'paired_test_questions': len(by_case),
                       **{metric: dict(Counter('improved' if p[metric] > 0 else 'regressed' if p[metric] < 0
                                               else 'tied' for p in paired
                                               if p['documents'] == trial['documents'] and p['seed'] == trial['seed']))
                          for metric in ('recall@1_delta', 'recall@5_delta', 'mrr_delta')}})
    groups = defaultdict(list)
    for row in all_rows:
        for subset in (row['split'], f"{row['split']}:{row['question_type']}",
                       f"{row['split']}:{'multi_gold' if len(row['gold_chunk_ids']) > 1 else 'single_gold'}"):
            groups[(row['documents'], subset, row['method'])].append(row)
    aggregate = []
    for (size, subset, method), rows in sorted(groups.items()):
        seed_values = {}
        for seed in report['manifest']['seeds']:
            current = [r for r in rows if r['seed'] == seed]
            seed_values[str(seed)] = {m: statistics.mean(r['metrics'][m] for r in current)
                                     for m in rows[0]['metrics']}
        aggregate.append({'documents': size, 'subset': subset, 'method': method,
                          'unique_questions': len({r['question_id'] for r in rows}),
                          'attempted': len(rows), 'failed': sum(r['error'] is not None for r in rows),
                          'metrics_mean_over_seeds': {m: statistics.mean(r['metrics'][m] for r in rows)
                                                      for m in rows[0]['metrics']},
                          'per_seed': seed_values,
                          'parent_document_coverage': statistics.mean(r['parent_document_coverage'] for r in rows),
                          'retrieval_ms': distribution([r['elapsed_ms'] for r in rows])})
    scale_pairs = []
    for seed in report['manifest']['seeds']:
        for size in report['manifest']['sizes'][1:]:
            smallest = min(report['manifest']['sizes'])
            qids = [qid for n, s, qid in question_pairs if n == size and s == seed]
            for qid in qids:
                for method in ('vector_matched', 'hybrid', 'bm25_only'):
                    before = question_pairs[(smallest, seed, qid)][method]
                    after = question_pairs[(size, seed, qid)][method]
                    scale_pairs.append({'seed': seed, 'from_documents': smallest, 'to_documents': size,
                                        'question_id': qid, 'method': method,
                                        **{f'{m}_delta': after['metrics'][m] - before['metrics'][m]
                                           for m in ('recall@1', 'recall@5', 'mrr')}})
    analysis = {'aggregate': aggregate, 'paired_counts': trials,
                'paired_family_intervals': [{'documents': size, 'metric': metric,
                    **paired_interval([r for r in all_rows if r['documents'] == size and r['split'] == 'test'], metric)}
                    for size in report['manifest']['sizes'] for metric in ('recall@1', 'recall@5', 'mrr')],
                'total_retrieval_attempts': len(all_rows),
                'failed_retrieval_attempts': sum(r['error'] is not None for r in all_rows),
                'hybrid_incomplete_test_attempts': len(failures),
                'failure_counts': {'missing_both_routes': sum(bool(f['missing_both_routes']) for f in failures),
                    'candidate_present_final_missing': sum(bool(f['present_candidates_but_missing_final']) for f in failures)}}
    write_json(args.run_dir / 'analysis.json', analysis)
    write_json(args.run_dir / 'paired-per-question.json', paired)
    write_json(args.run_dir / 'scale-change-per-question.json', scale_pairs)
    write_json(args.run_dir / 'hybrid-failures.json', failures)
    print(json.dumps({'total_retrieval_attempts': len(all_rows), 'failed': analysis['failed_retrieval_attempts'],
                      'test': [a for a in aggregate if a['subset'] == 'test'],
                      'intervals': analysis['paired_family_intervals']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
