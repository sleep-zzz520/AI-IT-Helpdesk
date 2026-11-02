"""Protocol checks: isolated changes and honest failed-pair accounting."""
import json
import subprocess

from scripts import eval_sync_resilience
from scripts.eval_sync_comparison import mutate, source_body, summarize


def test_mutations_are_independent_and_metadata_preserves_body_and_identity():
    files = {f'{i}.md': f'---\ndocument_id: id-{i}\nscenario: vpn\n---\n## Step\nBody {i}\n'
             for i in range(100)}
    metadata, stats = mutate(files, 'metadata_10pct', 17)
    assert stats['changed_documents'] == 10
    assert sum(metadata[p] != files[p] for p in files) == 10
    assert all(source_body(metadata[p]) == source_body(files[p]) for p in files)
    renamed, stats = mutate(files, 'rename_10pct', 17)
    assert len(renamed) == 100 and sorted(renamed.values()) == sorted(files.values())
    assert sum(p.startswith('renamed/') for p in renamed) == 10
    changed, stats = mutate(files, 'body_1pct', 17)
    assert stats['changed_documents'] == 1
    assert sum(changed[p] != files[p] for p in files) == 1
    # No mutation may warm or alter a later case's source fixture.
    assert all('review_revision' not in text and '新增记录' not in text for text in files.values())


def test_summary_exposes_failures_and_uses_only_complete_pairs():
    def row(version, repeat, ok, wall=10):
        phase = {'wall_ms': wall, 'cpu_ms': 1, 'peak_rss_bytes': 100,
                 'logical_storage_bytes': 100, 'counts': {'embedding_inputs': 0}}
        return {'documents': 64, 'scenario': 'unchanged', 'version': version,
                'repeat': repeat, 'ok': ok, 'phase': phase}
    rows = [row('baseline', 0, True, 20), row('current', 0, True, 10),
            row('baseline', 1, True, 100), row('current', 1, False)]
    result = summarize(rows)[0]
    assert result['attempted'] == {'baseline': 2, 'current': 2}
    assert result['failed'] == {'baseline': 0, 'current': 1}
    assert result['paired_wall_speedup']['n'] == 1
    assert result['paired_wall_speedup']['median'] == 2
    assert result['paired_wall_speedup']['bootstrap_95pct_interval'] is None


def test_fault_matrix_records_each_timeout_and_continues(monkeypatch, tmp_path):
    output = tmp_path / 'faults.json'
    monkeypatch.setattr('sys.argv', ['eval_sync_resilience', '--input-corpus', str(tmp_path / 'input.json'),
                                   '--backend-root', str(tmp_path / 'backend'), '--output', str(output)])

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])

    monkeypatch.setattr(eval_sync_resilience.subprocess, 'run', timeout)
    assert eval_sync_resilience.main() == 1
    report = json.loads(output.read_text())
    assert [row['case'] for row in report['cases']] == list(eval_sync_resilience.CASES)
    assert all(not row['passed'] and row['error']['type'] == 'TimeoutExpired' for row in report['cases'])
    assert report['ok'] is False
