"""Paired historical Git-incremental benchmark, isolated from live data and APIs.

Both full source revisions run against the same installed dependencies. Each
mutation forks an exited warm baseline, so case order never warms another case.
The embedding function is replaced; SQLite, Chroma and publication run. Provider
batching/retry/latency and bills are outside this experiment.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ('unchanged', 'body_1pct', 'body_10pct', 'metadata_10pct',
             'rename_10pct', 'delete_10pct', 'parser_source_change')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def commit_for(seed, name):
    return hashlib.sha1(f'{seed}:{name}'.encode(), usedforsecurity=False).hexdigest()


def source_body(text):
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        raise ValueError('source has no frontmatter')
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == '---')
    return ''.join(lines[end + 1:]).lstrip('\r\n')


def corpus(count, seed, corpus_root):
    """Actual local IT documents plus length-stratified external prose.

    Preserve source body bytes; add controlled identity/frontmatter only. The
    external corpus is a storage workload, not an IT answer-quality dataset.
    """
    if not 16 <= count <= 512:
        raise ValueError('document count must be between 16 and the actual 512 limit')
    rng = random.Random(seed)
    candidates = sorted(corpus_root.glob('*.md'))
    if len(candidates) < count:
        raise ValueError('local external corpus is missing or too small')
    ordered = sorted(candidates, key=lambda p: (p.stat().st_size, p.name))
    # Reserve explicit long-tail samples; use equal length-quartile strata for
    # the rest. This is a stress distribution, not a claimed production mix.
    needed = count - 16
    tail_n = min(needed, max(1, needed // 16)) if needed else 0
    tail_pool = ordered[max(0, len(ordered) * 99 // 100):]
    selected = rng.sample(tail_pool, min(tail_n, len(tail_pool)))
    chosen = set(selected)
    quartiles = [ordered[len(ordered) * i // 4:len(ordered) * (i + 1) // 4] for i in range(4)]
    while len(selected) < needed:
        for group in quartiles:
            available = [p for p in group if p not in chosen]
            if available and len(selected) < needed:
                p = rng.choice(available)
                selected.append(p)
                chosen.add(p)
    files, provenance = {}, []
    legacy_ids = json.loads((ROOT / 'backend/app/rag/legacy_document_ids.json').read_text())
    it_paths = sorted((ROOT / 'docs/knowledge').rglob('*.md'))
    if len(it_paths) != 16:
        raise ValueError('benchmark protocol requires the recorded 16 IT documents')
    for p in it_paths:
        path = p.relative_to(ROOT / 'docs/knowledge').as_posix()
        text = p.read_text(encoding='utf-8')
        files[path] = (text if '\ndocument_id:' in text.split('---', 2)[1]
                       else text.replace('---\n', f'---\ndocument_id: {legacy_ids[path]}\n', 1))
        provenance.append({'input': path, 'source': f'docs/knowledge/{path}',
                           'source_sha256': digest(text), 'kind': 'IT Markdown'})
    for p in selected:
        raw = p.read_text(encoding='utf-8')
        body = source_body(raw)
        path = f'external/{p.name}'
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f'local-crud-rag/{p.name}'))
        files[path] = (f'---\ndocument_id: {identity}\nscenario: scale\n'
                       f'title: "External corpus {p.stem}"\nstatus: active\n'
                       'valid_to: "2099-12-31"\n---\n' + body)
        provenance.append({'input': path, 'source': p.name, 'source_sha256': digest(raw),
                           'body_sha256': digest(body), 'kind': 'local CRUD-RAG derivative'})
    sizes = sorted(len(text.encode()) for text in files.values())
    total = sum(sizes)
    if total > 16 * 1024 * 1024 or max(sizes) > 2 * 1024 * 1024:
        raise ValueError('sample exceeds unchanged production source limits')
    return files, {'seed': seed, 'documents': len(files), 'utf8_bytes': total,
                   'file_bytes_min': min(sizes), 'file_bytes_median': statistics.median(sizes),
                   'file_bytes_max': max(sizes),
                   'unique_bodies': len({digest(source_body(t)) for t in files.values()}),
                   'sampling': '16 IT + equal length quartiles + 1/16 explicit top-1% tail',
                   'manifest': provenance,
                   'input_manifest_sha256': digest(json.dumps(files, ensure_ascii=False, sort_keys=True))}


def mutate(files, name, seed):
    result = dict(files)
    if name in {'unchanged', 'parser_source_change'}:
        return result, {'changed_documents': 0}
    ratio = .01 if name == 'body_1pct' else .1
    selected = random.Random(seed).sample(sorted(files), max(1, math.ceil(len(files) * ratio)))
    for index, path in enumerate(selected):
        if name.startswith('body_'):
            result[path] += f'\n\n本轮隔离测评新增记录 {seed}-{index}：变更须经复核，旧结论不再适用。\n'
        elif name == 'metadata_10pct':
            # Distinct metadata field; preserve body byte-for-byte.
            result[path] = result[path].replace('---\n', f'---\nreview_revision: "{seed}-{index}"\n', 1)
        elif name == 'rename_10pct':
            result[f'renamed/{path}'] = result.pop(path)
        elif name == 'delete_10pct':
            result.pop(path)
        else:
            raise ValueError(name)
    return result, {'changed_documents': len(selected), 'ratio': len(selected) / len(files)}


def export_backend(ref, target):
    sha = subprocess.check_output(['git', 'rev-parse', '--verify', f'{ref}^{{commit}}'], cwd=ROOT, text=True).strip()
    raw = subprocess.check_output(['git', 'archive', '--format=tar', sha, 'backend'], cwd=ROOT)
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(target, filter='data')
    return sha, target / 'backend'


def quantiles(values):
    ordered = sorted(values)
    quartiles = statistics.quantiles(ordered, n=4, method='inclusive') if len(ordered) > 1 else ordered * 3
    return {'n': len(ordered), 'median': statistics.median(ordered), 'q1': quartiles[0],
            'q3': quartiles[2], 'min': ordered[0], 'max': ordered[-1]}


def summarize(rows):
    groups = {}
    for row in rows:
        groups.setdefault((row['documents'], row['scenario']), {}).setdefault(row['version'], []).append(row)
    summaries = []
    for (count, scenario), versions in sorted(groups.items()):
        item = {'documents': count, 'scenario': scenario, 'versions': {},
                'attempted': {label: len(trials) for label, trials in versions.items()},
                'failed': {label: sum(not t['ok'] for t in trials) for label, trials in versions.items()}}
        for label, all_trials in versions.items():
            trials = [t for t in all_trials if t['ok']]
            if not trials:
                continue
            item['versions'][label] = {
                key: quantiles([r['phase'][key] for r in trials])
                for key in ('wall_ms', 'cpu_ms', 'peak_rss_bytes', 'logical_storage_bytes')}
            item['versions'][label]['counts'] = {
                key: quantiles([r['phase']['counts'][key] for r in trials])
                for key in trials[0]['phase']['counts']}
        paired = {label: {r['repeat']: r for r in trials if r['ok']} for label, trials in versions.items()}
        if 'baseline' in paired and 'current' in paired:
            pairs = sorted(set(paired['baseline']) & set(paired['current']))
            if not pairs:
                summaries.append(item)
                continue
            ratios = [paired['baseline'][i]['phase']['wall_ms'] / paired['current'][i]['phase']['wall_ms'] for i in pairs]
            interval = None
            if len(ratios) >= 3:
                rng = random.Random(1749)
                boots = sorted(statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(2000))
                interval = [boots[49], boots[1949]]
            item['paired_wall_speedup'] = {**quantiles(ratios),
                'bootstrap_95pct_interval': interval,
                'interpretation': 'baseline/current; >1 faster current; small-n interval exploratory'}
        summaries.append(item)
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', type=int, nargs='+', default=[64, 256, 512])
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--seed', type=int, default=20261102)
    parser.add_argument('--baseline', default='576d093')
    parser.add_argument('--current', default='HEAD')
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument('--corpus-root', type=Path, default=ROOT / '数据集/scale_corpus')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--trial-timeout', type=int, default=300)
    parser.add_argument('--profile-mode', choices=('native', 'precomputed'), default='native')
    args = parser.parse_args()
    if args.repeats < 1 or len(set(args.sizes)) != len(args.sizes):
        parser.error('positive repeats and unique sizes required')
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'report.json').exists():
        parser.error('report already exists; choose a new output directory')
    worker = Path(__file__).with_name('sync_benchmark_worker.py')
    started = time.monotonic()
    rows, datasets = [], []
    report = {'profile_mode': args.profile_mode,
              'profile_scope': ('native fingerprint work included' if args.profile_mode == 'native' else 'historical precomputed fingerprints; profile recomputation excluded'),
              'protocol': 'paired-git-incremental-v2', 'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
              'python': sys.version, 'platform': platform.platform(), 'sizes': args.sizes,
              'repeats': args.repeats, 'seed': args.seed, 'scenarios': args.scenarios,
              'scope': 'historical full backend revisions, same installed dependencies; real SQLite/Chroma; deterministic 1024-D embedding',
              'cache_control': 'each mutation copies an exited same-revision same-repeat cold-build template; application-warm, OS cache uncontrolled',
              'order_control': 'paired AB/BA alternation, sequential children; no concurrent benchmark CPU contention',
              'timing_scope': 'enqueue through validated publish; excludes source fixture generation, process imports, directory copy and independent oracle',
              'limitations': ['not MySQL or provider latency/cost', 'external corpus is not IT QA ground truth',
                             f'{args.repeats} repeats are exploratory; no stable p95 claim', 'logical file bytes are not allocated disk blocks',
                             'same runtime dependencies, not reconstructed historical environment'],
              'trials': rows, 'datasets': datasets}
    with tempfile.TemporaryDirectory(prefix='sync-paired-') as scratch:
        root = Path(scratch)
        revisions = {}
        for label, ref in (('baseline', args.baseline), ('current', args.current)):
            sha, backend = export_backend(ref, root / f'source-{label}')
            upgraded = root / f'source-{label}-parser-change'
            shutil.copytree(backend, upgraded)
            with (upgraded / 'app/rag/git_source.py').open('a') as handle:
                handle.write('\n# Evaluation-only nonsemantic parser source revision.\n')
            revisions[label] = {'sha': sha, 'backend': backend, 'upgraded': upgraded}
        report['revisions'] = {label: value['sha'] for label, value in revisions.items()}

        def checkpoint():
            report.update(duration_seconds=round(time.monotonic() - started, 3), summary=summarize(rows),
                          attempted_trials=len(rows), failed_trials=sum(not r['ok'] for r in rows))
            write_json(output / 'report.json', report)

        def trial(label, size, repeat, scenario, seed, files, work, resume=None, details=None):
            key = f'n{size}-r{repeat}-{label}-{scenario}'
            payload = {'seed': seed, 'repeat': repeat, 'version_label': label,
                       'phases': [{'name': scenario, 'commit': commit_for(seed, scenario), 'files': files}]}
            if resume:
                shutil.copytree(resume, work)
                payload['resume_from'] = str(resume)
            inp, out = output / 'inputs' / f'{key}.json', output / 'trials' / f'{key}.json'
            # Input fixtures are external/local corpus derivatives: keep only in
            # requested output directory, never implicitly add them to Git.
            write_json(inp, payload)
            backend = revisions[label]['upgraded' if scenario == 'parser_source_change' else 'backend']
            command = [sys.executable, str(worker), '--backend-root', str(backend), '--input', str(inp),
                       '--output', str(out), '--work-dir', str(work), '--profile-mode', args.profile_mode]
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'ANONYMIZED_TELEMETRY': 'False'}
            with (output / f'{key}.log').open('w') as log:
                try:
                    process = subprocess.run(command, cwd=root, env=env, stdout=log, stderr=log, timeout=args.trial_timeout)
                    result = json.loads(out.read_text()) if out.exists() else {'ok': False, 'error': {'type': 'MissingReport'}}
                    result['exit_code'] = process.returncode
                except subprocess.TimeoutExpired:
                    result = {'ok': False, 'error': {'type': 'TrialTimeout', 'seconds': args.trial_timeout}}
                    write_json(out, result)
            row = {'documents': size, 'repeat': repeat, 'scenario': scenario, 'version': label,
                   'seed': seed, 'profile_mode': args.profile_mode, 'ok': result['ok'], 'artifact': f'trials/{key}.json',
                   'mutation': details or {}, 'phase': result.get('phases', [{}])[-1] if result.get('phases') else {}}
            if not result['ok']:
                row['error'] = result.get('error')
            rows.append(row)
            checkpoint()
            print(json.dumps({k: row[k] for k in ('documents', 'repeat', 'scenario', 'version', 'ok')} |
                             {'wall_ms': row['phase'].get('wall_ms'), 'counts': row['phase'].get('counts')}, ensure_ascii=False), flush=True)
            return result['ok']

        for size in args.sizes:
            for repeat in range(args.repeats):
                seed = args.seed + repeat * 1009
                files, manifest = corpus(size, seed, args.corpus_root)
                manifest.update(repeat=repeat)
                datasets.append(manifest)
                write_json(output / 'manifests' / f'n{size}-r{repeat}.json', manifest)
                templates = {}
                labels = ['baseline', 'current'] if repeat % 2 == 0 else ['current', 'baseline']
                for label in labels:
                    work = root / f'n{size}-r{repeat}-{label}-warm'
                    ok = trial(label, size, repeat, 'cold', seed, files, work)
                    if ok:
                        templates[label] = work
                for scenario in args.scenarios:
                    changed, details = mutate(files, scenario, seed + 7)
                    for label in labels:
                        if label not in templates:
                            continue
                        work = root / f'n{size}-r{repeat}-{label}-{scenario}'
                        trial(label, size, repeat, scenario, seed, changed, work, templates[label], details)
                        shutil.rmtree(work)
                for template in templates.values():
                    shutil.rmtree(template)
        checkpoint()
    print(json.dumps({'report': str(output / 'report.json'), 'trials': len(rows),
                      'passed': sum(row['ok'] for row in rows), 'duration_seconds': report['duration_seconds']}, ensure_ascii=False))
    return 0 if all(row['ok'] for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
