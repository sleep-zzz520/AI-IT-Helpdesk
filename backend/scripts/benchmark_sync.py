"""离线合成同步基准：临时 SQLite/Chroma，禁止真实模型与生产数据库。

运行：cd backend && ../.venv/bin/python -m scripts.benchmark_sync --docs 100
默认输出 stdout；--output path 可保存 JSON。时间是本机观测，不是生产 SLA。
"""
import argparse
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.rag import sync
from app.rag.store import ChromaStore


def benchmark(count=100):
    if count < 1:
        raise ValueError('docs must be positive')
    with tempfile.TemporaryDirectory(prefix='itdesk-sync-offline-') as directory:
        root = Path(directory)
        docs = root/'docs'; docs.mkdir()
        engine = create_engine(f'sqlite:///{root / "ledger.sqlite"}')
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        calls = []

        def fake_embed(texts):
            calls.extend(texts)
            return [[1.0, 0.0] for _ in texts]

        def run(label, store, clear=False):
            calls.clear()
            if clear:
                store.clear()
            start = time.perf_counter()
            report = sync.run_sync(docs, store=store, kb_name='offline_benchmark')
            return {'label': label, 'embedded_texts': len(calls),
                    'elapsed_seconds': round(time.perf_counter()-start, 6),
                    'chunks': store.count(), 'added': len(report.added),
                    'updated': len(report.updated), 'deleted': len(report.deleted),
                    'failed': report.failed}

        for index in range(count):
            (docs/f'{index}.md').write_text(
                f'---\nscenario: synthetic\n---\n# Document {index}\n\n## Procedure\nSynthetic step {index}.',
                encoding='utf-8')
        store = ChromaStore(str(root/'vectors'), 'offline_incremental')
        candidate = ChromaStore(str(root/'vectors'), 'offline_candidate')
        try:
            with patch.object(sync, 'SessionLocal', sessions), patch.object(sync, 'embed_texts', fake_embed):
                initial = run('initial', store)
                (docs/'0.md').write_text('---\nscenario: synthetic\n---\n# Document 0\n\n## Procedure\nChanged step.', encoding='utf-8')
                changed = run('one_changed_document', store)
                rebuild = run('empty_candidate_full_rebuild', candidate, clear=True)
                (docs/'0.md').write_text('invalid frontmatter', encoding='utf-8')
                corrupt = run('existing_document_parse_failure', store)
                # 精确核对损坏文档身份，不以其他文档是否存在代替。
                from app.rag.loader import doc_id_of
                preserved = len(store.get_doc_ids(doc_id_of('0.md')))
        finally:
            engine.dispose()
        return {'kind': 'synthetic_offline', 'docs': count,
                'embedding': 'fake constant vectors; no network calls',
                'storage': 'temporary SQLite and Chroma; removed after run',
                'measure': 'embedding input count, including duplicate probe; elapsed local observation',
                'initial': initial, 'incremental': changed, 'candidate_rebuild': rebuild,
                'incremental_embedding_saving_percent': round(100*(1-changed['embedded_texts']/rebuild['embedded_texts']), 4),
                'corrupt_existing_source': {**corrupt, 'preserved_document_chunks': preserved}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs', type=int, default=100)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = benchmark(args.docs)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload+'\n', encoding='utf-8')
    print(payload)


if __name__ == '__main__':
    main()
