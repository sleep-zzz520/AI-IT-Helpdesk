"""Current-version fault correctness drills; isolated subprocess per case, no performance claims."""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import traceback
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

CASES = ('stage_corruption', 'vector_corruption', 'transient_503', 'permanent_401',
         'partial_upsert', 'incomplete_candidate', 'finite_candidate_vector_change',
         'superseded_prepared', 'delete_failed_build_rollback')


def child(args, root):
    spec = importlib.util.spec_from_file_location('isolated_benchmark_helpers', Path(__file__).with_name('sync_benchmark_worker.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    payload = json.loads(args.input_corpus.read_text())
    files = payload['phases'][0]['files']
    assert len(files) >= 2, 'at least two documents required'
    baseline_input = root / 'baseline-input.json'
    baseline_input.write_text(json.dumps({'phases': [{'name': 'baseline', 'commit': '1' * 40, 'files': files}]}))
    baseline = {}
    work = root / 'work'
    helper.run(SimpleNamespace(backend_root=args.backend_root, work_dir=work, input=baseline_input), baseline)
    from app import db
    from app.config import settings
    from app.models import KbSource, KbSyncJob
    from app.rag import embedder, git_source
    from app.rag import generations as g
    from app.rag.store import ChromaStore
    calls = {'attempts': 0, 'embedding_inputs': 0}
    def embed(texts):
        calls['attempts'] += 1
        calls['embedding_inputs'] += len(texts)
        return [helper.deterministic_vector(text) for text in texts]
    embedder.embed_texts = embed
    def active():
        with db.SessionLocal() as session:
            return session.get(KbSource, settings.KB_SOURCE_ID).active_job
    def raw(job_id):
        with db.SessionLocal() as session:
            collection = session.get(KbSyncJob, job_id).collection
        store = ChromaStore(settings.KB_VECTOR_DIR, collection, must_exist=True)
        return store, store._col.get(include=['documents', 'metadatas'])
    old = active()
    _, old_raw = raw(old)
    def old_unchanged():
        assert active() == old
        assert raw(old)[1] == old_raw
        helper.verify(db, g, settings, ChromaStore, helper.expected_documents(files), old)
    sequence = 1
    def enqueue(values):
        nonlocal sequence
        sequence += 1
        snap = git_source.read_snapshot_from_files(f'{sequence:040x}', {p: text.encode() for p, text in values.items()})
        return g.enqueue(snap)
    def publish(job, values):
        assert job['status'] == 'prepared', job
        g.publish(job['id'])
        return helper.verify(db, g, settings, ChromaStore, helper.expected_documents(values), job['id'])
    def rejected(job_id):
        try:
            g.publish(job_id)
        except (ValueError, RuntimeError):
            return True
        raise AssertionError('unsafe candidate publication was accepted')
    changed = dict(files)
    first, second = sorted(files)[:2]
    changed[first] += '\n## Resilience change\nUnique changed recovery evidence 9361.\n'
    result = {'case': args.case, 'old_active': old, 'baseline_oracle': baseline['phases'][0]['oracle'],
              'scope': 'real SQLite/Chroma and sync lifecycle; deterministic embedding; correctness only'}
    case_passed = True
    if args.case == 'stage_corruption':
        paths = list((work / 'state' / 'stages').rglob('*.json'))
        assert paths
        for path in paths:
            path.write_text('{corrupted')
        job = g.process_job(enqueue(files)['id'])
        result['success_oracle'] = publish(job, files)
        assert calls['embedding_inputs'] == 0
        result.update(injection='all persistent parse/chunk JSON caches truncated',
                      predicates=['source text and metadata restored from authoritative export', 'no additional embedding inputs'])
    elif args.case == 'vector_corruption':
        # Rebuilding an invalid vector in an unchanged doc can use active-generation reuse;
        # force one source revision via metadata-only change, preserving embedding inputs.
        changed = dict(files)
        changed[first] = files[first].replace('---\n', '---\nresilience_note: vector-cache-check\n', 1)
        paths = list((work / 'state' / 'embeddings').rglob('*.json'))
        assert paths
        for path in paths:
            path.write_text('{corrupted')
        job = g.process_job(enqueue(changed)['id'])
        result['success_oracle'] = publish(job, changed)
        assert calls['embedding_inputs'] > 0
        result.update(injection='all embedding cache JSON truncated plus one metadata-only revision',
                      predicates=['invalid vectors recomputed', 'raw vectors and metadata match independent oracle'])
    elif args.case in ('transient_503', 'permanent_401'):
        import httpx
        from openai import APIStatusError
        status = 503 if args.case == 'transient_503' else 401
        attempts = 0
        def failing(texts):
            nonlocal attempts
            attempts += 1
            if status == 401 or attempts == 1:
                request = httpx.Request('POST', 'https://example.invalid/embeddings')
                raise APIStatusError('synthetic injected fault', response=httpx.Response(status, request=request), body={})
            return embed(texts)
        embedder.embed_texts = failing
        job = g.process_job(enqueue(changed)['id'])
        old_unchanged()
        if status == 503:
            assert job['status'] == 'queued' and job['next_attempt_at'] and job['attempts'] == 1
            result['persisted_retry_before_clock_advance'] = {key: job.get(key) for key in ('status', 'attempts', 'next_attempt_at', 'error_kind', 'failures')}
            with db.SessionLocal() as session:
                row = session.get(KbSyncJob, job['id'])
                assert row.next_attempt_at > g._now()
            assert g.process_job(job['id'])['attempts'] == 1
            # Advance application clock, not a measurement of actual retry recovery latency.
            original_now = g._now
            with db.SessionLocal() as session:
                due = session.get(KbSyncJob, job['id']).next_attempt_at
            g._now = lambda: max(original_now(), due + timedelta(seconds=1))
            job = g.process_job(job['id'])
            result['success_oracle'] = publish(job, changed)
            assert job['attempts'] == 2
            result['clock_injection'] = 'application clock advanced beyond persisted next_attempt_at; not recovery latency'
        else:
            assert job['status'] == 'failed' and job['attempts'] == 1
            assert g.process_job(job['id'])['status'] == 'failed' and attempts == 1
            rejected(job['id'])
        result.update(injection=f'APIStatusError HTTP {status} at embedding boundary', embedding_attempts=attempts,
                      predicates=['old raw generation unchanged during failure', 'persistent retry policy respected'])
    elif args.case == 'partial_upsert':
        original = ChromaStore.upsert_chunks
        def partial(self, chunks, vectors):
            original(self, chunks[:1], vectors[:1])
            raise PermissionError('synthetic index write failure after one persisted chunk')
        ChromaStore.upsert_chunks = partial
        job = g.process_job(enqueue(changed)['id'])
        assert job['status'] == 'failed'
        old_unchanged()
        _, candidate = raw(job['id'])
        assert candidate['ids']
        rejected(job['id'])
        result.update(injection='persist first candidate block then PermissionError', candidate_blocks=len(candidate['ids']),
                      predicates=['physical partial candidate exists', 'old raw/served state unchanged', 'failed publish refused'])
    elif args.case == 'incomplete_candidate':
        job = g.process_job(enqueue(changed)['id'])
        assert job['status'] == 'prepared'
        candidate, contents = raw(job['id'])
        candidate._col.delete(ids=[contents['ids'][0]])
        rejected(job['id'])
        old_unchanged()
        result.update(injection='delete one raw block after prepared before publish',
                      predicates=['manifest-to-raw completeness prevents publication', 'old generation unchanged'])
    elif args.case == 'finite_candidate_vector_change':
        from app.rag.chunks import Chunk
        job = g.process_job(enqueue(changed)['id'])
        assert job['status'] == 'prepared'
        candidate, contents = raw(job['id'])
        cid, text, metadata = next((cid, text, meta) for cid, text, meta in
                                   zip(contents['ids'], contents['documents'], contents['metadatas'], strict=True)
                                   if meta['chunk_type'] == 'child')
        candidate.upsert_chunks([Chunk(cid, text, metadata)], [[-1.0] + [0.0] * 1023])
        try:
            g.publish(job['id'])
        except (ValueError, RuntimeError):
            old_unchanged()
            rejected_change = True
        else:
            rejected_change = False
            case_passed = False
            try:
                helper.verify(db, g, settings, ChromaStore, helper.expected_documents(changed), job['id'])
            except AssertionError as exc:
                assert exc.args and exc.args[0] == ('child vector', cid), ('unexpected oracle failure', exc.args)
                result['independent_oracle_error'] = str(exc)
            else:
                raise AssertionError('independent vector oracle failed to detect injected change')
        result.update(injection='replace one prepared child embedding with a finite, valid-1024-D but incorrect vector',
                      publication_rejected=rejected_change,
                      predicates=['changed candidate vector must not be published'],
                      observed='publication blocked' if rejected_change else 'incorrect vector published; independent oracle detected mismatch')
    elif args.case == 'superseded_prepared':
        job = g.process_job(enqueue(changed)['id'])
        assert job['status'] == 'prepared'
        newer = dict(changed)
        newer[first] += '\nNewer desired intent 472.\n'
        new_job = enqueue(newer)
        rejected(job['id'])
        old_unchanged()
        result['success_oracle'] = publish(g.process_job(new_job['id']), newer)
        result.update(injection='enqueue newer source intent after old candidate prepared',
                      predicates=['superseded prepared publication refused', 'new desired version publishes correctly'])
    elif args.case == 'delete_failed_build_rollback':
        # Create previous+active generations, then delete one doc while another fails embedding.
        second_job = g.process_job(enqueue(files)['id'])
        publish(second_job, files)
        before_failed = active()
        deleted_id = next(identity for identity, item in helper.expected_documents(files).items() if item['path'] == first)
        deletion = dict(files)
        del deletion[first]
        deletion[second] += '\n## New revision\nEmbedding must fail for this revision.\n'
        def unavailable(texts):
            calls['attempts'] += 1
            raise PermissionError('synthetic embedding failure')
        embedder.embed_texts = unavailable
        job = g.process_job(enqueue(deletion)['id'])
        assert job['status'] == 'failed' and active() == before_failed
        assert deleted_id in g.denied_ids()
        physical = {m['doc_id'] for m in raw(before_failed)[1]['metadatas']}
        served = {item['metadata']['doc_id'] for item in g.resolve_store().get_all_meta().values()}
        assert deleted_id in physical and deleted_id not in served
        with db.SessionLocal() as session:
            source = session.get(KbSource, settings.KB_SOURCE_ID)
            rollback_target, before_seq = source.previous_job, source.desired_seq
            assert rollback_target and rollback_target != before_failed
        rolled_back = g.rollback()
        assert rolled_back['id'] == rollback_target and active() == rollback_target
        with db.SessionLocal() as session:
            source = session.get(KbSource, settings.KB_SOURCE_ID)
            assert source.previous_job == before_failed and source.desired_seq == before_seq + 1
        served_after = {item['metadata']['doc_id'] for item in g.resolve_store().get_all_meta().values()}
        assert deleted_id not in served_after and deleted_id in g.denied_ids()
        result.update(injection='delete one source and fail embedding of another before publish, then rollback',
                      rollback_target=rollback_target, rollback_completed=True,
                      physical_doc_ids=sorted(physical), served_doc_ids=sorted(served), served_after_rollback=sorted(served_after),
                      predicates=['failed build retains prior active generation', 'authoritative deny hides physically retained deletion', 'rollback does not resurrect deleted document'])
    _, active_raw = raw(active())
    denied = set(g.denied_ids())
    expected_served = {cid: text for cid, text, meta in zip(active_raw['ids'], active_raw['documents'], active_raw['metadatas'], strict=True) if meta['doc_id'] not in denied}
    served_rows = g.resolve_store().get_all_meta()
    store = g.resolve_store()
    assert {cid: item['text'] for cid, item in served_rows.items()} == expected_served
    assert store.get_by_ids(active_raw['ids']) == expected_served
    remaining_children = {cid: (text, meta) for cid, text, meta in
                          zip(active_raw['ids'], active_raw['documents'], active_raw['metadatas'], strict=True)
                          if meta['doc_id'] not in denied and meta['chunk_type'] == 'child'}
    assert remaining_children
    sample_text = next(iter(remaining_children.values()))[0]
    hits = store.query(helper.deterministic_vector(embedder._clean_text(sample_text)),
                       top_k=len(active_raw['ids']), where={'chunk_type': 'child'})
    assert {hit.id for hit in hits} == set(remaining_children)
    assert all(hit.metadata['doc_id'] not in denied for hit in hits)
    result['raw_served_check'] = {'raw_blocks': len(active_raw['ids']), 'served_blocks': len(served_rows),
                                'denied_doc_ids': sorted(denied),
                                'served_metadata_view_matches_raw_minus_deny': True,
                                'get_by_ids_matches_raw_minus_deny': True,
                                'raw_query_matches_all_remaining_children': True}
    result['job_report'] = job.get('report')
    result.update(passed=case_passed, new_active=active(), embedding_counters=calls,
                  job_status=g.get_job(job['id'])['status'], task_attempts=job.get('attempts'), physical_served_checks=True)
    db.engine.dispose()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--case', choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    report = {'scope': 'fault correctness only; no real model/API; separate temporary SQLite/Chroma per case', 'cases': [], 'ok': False}
    try:
        if args.case:
            with tempfile.TemporaryDirectory(prefix='sync-resilience-') as directory:
                report = child(args, Path(directory))
        else:
            with tempfile.TemporaryDirectory(prefix='sync-resilience-reports-') as directory:
                for case in CASES:
                    output = Path(directory) / f'{case}.json'
                    command = [sys.executable, str(Path(__file__).resolve()), '--input-corpus', str(args.input_corpus.resolve()),
                               '--backend-root', str(args.backend_root.resolve()), '--output', str(output), '--case', case]
                    try:
                        completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
                        item = json.loads(output.read_text()) if output.exists() else {'case': case, 'passed': False, 'error': completed.stderr[-4000:]}
                    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                        item = {'case': case, 'passed': False, 'error': {'type': type(exc).__name__}}
                    report['cases'].append(item)
            assert [item['case'] for item in report['cases']] == list(CASES)
            report['ok'] = all(item.get('passed') for item in report['cases'])
    except BaseException as exc:
        report.update(passed=False, error={'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()})
        if args.case:
            report['case'] = args.case
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get('ok') or report.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
