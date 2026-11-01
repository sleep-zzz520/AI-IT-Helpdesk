"""真实29题隔离评测；在 backend 以 ../.venv/bin/python -m scripts.eval_isolated_qa 执行。
不改生产库、active指针或项目配置。父进程硬限制总耗时600秒；逐题报告保留。
默认真实模型沿用当前 glm-5.3-flash；Embedding-3 真实调用。需要有效本地.env。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def worker(root, skip_quality):
    root = Path(root)
    stage_path = root / 'stage.json'
    stage = 'imports'
    try:
        write(stage_path, {'stage': stage, 'status': 'running'})
        from app import llm
        from app.config import settings
        from app.rag import embedder
        from app.rag.loader import load_doc
        from app.rag.splitter import split_markdown
        from app.rag.store import ChromaStore
        from tests import qa_eval
        from tests.rag_eval_dataset import E2E_CASES
        # 仅隔离进程内收紧网络重试，不改变项目配置源或真实模型行为。
        llm.client = llm.client.with_options(timeout=10, max_retries=0)
        embedder._client = embedder._client.with_options(timeout=10, max_retries=0)
        embedder.MAX_RETRIES = 1
        if not settings.ZHIPU_API_KEY:
            raise ValueError('missing API key')
        if settings.RERANK_ENABLED:
            raise ValueError('reranker must be disabled')
        store = ChromaStore(str(root / 'chroma'), 'gate_eval_isolated')
        if store.count() == 0:
            stage = 'load_16_markdown_documents'
            write(stage_path, {'stage': stage, 'status': 'running'})
            paths = sorted(Path(settings.KB_ROOT).rglob('*.md'))
            if len(paths) != 16:
                raise ValueError('expected 16 markdown documents')
            chunks = []
            for path in paths:
                # 当前16篇都是纯Markdown；若未来引入图片，终止以免额外视觉API调用。
                if '![' in path.read_text():
                    raise ValueError('embedded media requires separate budget')
                doc = load_doc(path, Path(settings.KB_ROOT))
                if doc is None:
                    raise ValueError('invalid knowledge document')
                split = split_markdown(doc)
                chunks.extend(([split.parent] if split.parent else []) + split.children)
            stage = 'embedding_and_isolated_chroma'
            write(stage_path, {'stage': stage, 'status': 'running', 'documents': len(paths), 'chunks': len(chunks)})
            vectors = embedder.embed_texts([chunk.text for chunk in chunks])
            store.upsert_chunks(chunks, vectors)
        stage = 'real_29_question_evaluation'
        write(stage_path, {'stage': stage, 'status': 'running', 'cases': len(E2E_CASES), 'chunks': store.count()})
        report_path = root / 'qa_report.json'
        previous = json.loads(report_path.read_text()) if report_path.exists() else {}
        if previous:
            previous_model = previous.get('model', {})
            if (previous_model.get('answer_chain') != settings.GLM_MODELS
                    or previous_model.get('json_chain') != settings.GLM_JSON_MODELS):
                raise ValueError('评测缓存模型与当前模型不同，请使用新的 output-dir')
        resumed_rows = previous.get('results', [])
        cases = tuple(sorted(E2E_CASES, key=lambda case: case.expected_behavior != 'refuse'))
        original_answer = qa_eval.answer_question

        def deadline(_signal, _frame):
            raise TimeoutError('isolated per-question budget')

        def bounded_answer(*args, **kwargs):
            signal.signal(signal.SIGALRM, deadline)
            signal.setitimer(signal.ITIMER_REAL, 20)
            try:
                return original_answer(*args, **kwargs)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)

        qa_eval.answer_question = bounded_answer
        report = qa_eval.evaluate(cases, store, report_path, sleep_seconds=0,
                                  quality_metrics=not skip_quality, resume_results=resumed_rows)
        report['scope'] = '真实Embedding-3+glm-5.3-flash；默认质量评测不变，本轮可显式跳过额外质量指标'
        report['prior_run_protocol'] = {
            'completed_case_ids': [row['case_id'] for row in resumed_rows],
            'model': previous.get('model'),
            'quality_metrics_requested': previous.get('quality_metrics_requested', True),
        }
        report['per_question_budget_seconds'] = 20
        report['sdk_max_retries'] = 0
        report['prior_llm_calls'] = previous.get('llm_calls', 0)
        report['prior_duration_seconds'] = previous.get('duration_seconds', 0)
        write(report_path, report)
        write(stage_path, {'stage': stage, 'status': 'completed', 'cases_completed': report['cases_completed']})
    except Exception as exc:
        # 异常类型足够定位阶段，避免错误响应或连接串泄露凭证。
        write(stage_path, {'stage': stage, 'status': 'failed', 'error_type': type(exc).__name__})
        raise SystemExit(1) from exc


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='构建隔离16篇知识库，运行真实29题问答测评')
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--budget-seconds', type=int, default=600)
    parser.add_argument('--skip-quality-metrics', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.output_dir, args.skip_quality_metrics)
    else:
        if not 1 <= args.budget_seconds <= 600:
            parser.error('预算必须为1到600秒')
        root = args.output_dir or Path(tempfile.mkdtemp(prefix='real_qa_gate_', dir='/tmp'))
        root.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'ANONYMIZED_TELEMETRY': 'False',
               'LLM_REQUEST_TIMEOUT': '10', 'RERANK_ENABLED': 'false',
               'GLM_MODELS': 'glm-5.3-flash', 'GLM_MODELS_FAST': 'glm-5.3-flash',
               'GLM_JSON_MODELS': 'glm-5.3-flash'}
        started = time.monotonic()
        command = [sys.executable, __file__, '--worker', '--output-dir', str(root)]
        if args.skip_quality_metrics:
            command.append('--skip-quality-metrics')
        with (root / 'run.log').open('a') as log:
            proc = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            try:
                code = proc.wait(timeout=args.budget_seconds)
                status = 'completed' if code == 0 else 'failed'
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                status = 'budget_exhausted'
        summary = {'status': status, 'duration_seconds': round(time.monotonic()-started, 1),
                   'artifact_directory': root.name, 'hard_budget_seconds': args.budget_seconds,
                   'scope': '真实检索+judge+生成；已完成题缓存复用，跳过指标留空，故障不算行为通过',
                   'quality_metrics_requested': not args.skip_quality_metrics,
                   'stage': json.loads((root/'stage.json').read_text()) if (root/'stage.json').exists() else None}
        if (root/'qa_report.json').exists():
            report = json.loads((root/'qa_report.json').read_text())
            summary['cases_completed'] = report['cases_completed']
            summary['cases_requested'] = report['cases_requested']
        write(root/'summary.json', summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
