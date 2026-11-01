"""合成文件及临时数据库回归；所有模型调用均被替换。"""
import pytest
from docx import Document
from pptx import Presentation

from app.rag import loader, parsers, sync
from app.rag.chunks import KnowledgeDoc
from app.rag.store import ChromaStore


def test_docx_preamble_and_table_only(tmp_path):
    p = tmp_path / 'test.docx'
    d = Document(); d.add_paragraph('before heading'); d.add_heading('Heading', 1)
    d.add_paragraph('after heading'); d.save(p)
    result = parsers.parse_docx(p)
    assert result.sections == [('文档正文', 'before heading'), ('Heading', 'after heading')]
    d = Document(); d.add_table(rows=1, cols=1).cell(0, 0).text = 'table only'; d.save(p)
    assert 'table only' in parsers.parse_docx(p).sections[0][1]


def test_ppt_blank_page_does_not_shift_image_anchor(tmp_path, monkeypatch):
    p = tmp_path / 'slides.pptx'; prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[6])
    slide = prs.slides.add_slide(prs.slide_layouts[1]); slide.shapes.title.text = 'Page two'
    slide.placeholders[1].text = 'body'; prs.save(p)
    result = parsers.parse_pptx(p)
    assert len(result.sections) == 2
    monkeypatch.setattr(loader, '_transcribe_image_ref', lambda *a, **kw: 'IMAGE HERE')
    doc = loader._compose_media_doc('slides.pptx', 'pptx', result.sections,
                                    [parsers.ImageRef('image.png', b'x', 2)], p)
    assert doc.content.index('Page two') < doc.content.index('IMAGE HERE')


def test_pdf_mixed_pages_render_scanned_and_text_image_pages(tmp_path):
    import io

    import fitz
    from PIL import Image
    p = tmp_path / 'mixed.pdf'; image = io.BytesIO()
    Image.new('RGB', (10, 10), 'white').save(image, format='PNG')
    pdf = fitz.open()
    page = pdf.new_page(); page.insert_text((50, 50), 'long ordinary text ' * 10)
    page = pdf.new_page(); page.insert_image(fitz.Rect(0, 0, 100, 100), stream=image.getvalue())
    page = pdf.new_page(); page.insert_text((50, 50), 'text plus screenshot ' * 10)
    page.insert_image(fitz.Rect(0, 100, 100, 200), stream=image.getvalue())
    pdf.save(p); pdf.close()
    parsed = parsers.parse_pdf(p)
    assert len(parsed.sections) == 3
    assert [image.page for image in parsed.images] == [2, 3]


def test_image_transcription_failure_does_not_publish_partial_document(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, '_transcribe_image_ref', lambda *a, **kw: '')
    assert loader._compose_media_doc('a.pdf', 'pdf', [('page', 'text')],
                                    [parsers.ImageRef('p.png', b'x', 1)], tmp_path/'a.pdf') is None


def test_scan_reports_per_file_failure_and_missing_root(tmp_path):
    (tmp_path/'bad.md').write_bytes(b'\xff')
    assert loader.scan_docs(tmp_path).skipped == ['bad.md']
    with pytest.raises(FileNotFoundError):
        loader.scan_docs(tmp_path/'missing')


def test_failed_embedding_preserves_old_chunks(tmp_path, monkeypatch):
    store = ChromaStore(str(tmp_path/'chroma'), 'integrity')
    doc = KnowledgeDoc('a.md', 'doc', {'scenario': 'vpn'}, '# A\n\n## B\nold text')
    monkeypatch.setattr(sync, 'embed_texts', lambda texts: [[1., 0.] for _ in texts])
    sync._sync_doc(doc, 'old', store, sync.SyncReport())
    before = store.get_all()
    doc.content = '# Changed\n\n## B\nnew text'
    calls = 0
    def fail(texts):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [[1., 0.] for _ in texts]  # 去重探针成功，真正整篇嵌入失败
        raise RuntimeError('offline failure')
    monkeypatch.setattr(sync, 'embed_texts', fail)
    with pytest.raises(RuntimeError): sync._sync_doc(doc, 'new', store, sync.SyncReport())
    assert store.get_all() == before


def test_build_readiness_is_persistent_and_failed_build_cannot_publish(tmp_path, monkeypatch):
    target = ChromaStore(str(tmp_path/'chroma'), 'candidate')
    monkeypatch.setattr(sync, 'run_sync', lambda *a, **kw: sync.SyncReport(failed=[('a', 'bad')]))
    sync.build_candidate(target)
    assert not ChromaStore(str(tmp_path/'chroma'), 'candidate').is_build_ready()


def test_publication_lock_excludes_parallel_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(sync.settings, 'KB_VECTOR_DIR', str(tmp_path))
    with sync.publication_lock(), pytest.raises(RuntimeError, match='正在同步'):  # noqa: SIM117 二次进入是互斥测试目标
        with sync.publication_lock(): pass


def test_video_samples_end_of_long_clip(tmp_path):
    import av
    from PIL import Image

    from app.rag.video import extract_frames
    path = tmp_path/'long.mp4'
    with av.open(str(path), mode='w') as container:
        stream = container.add_stream('mpeg4', rate=3)
        stream.width = 32; stream.height = 32; stream.pix_fmt = 'yuv420p'
        for index in range(90):
            frame = av.VideoFrame.from_image(Image.new('RGB', (32, 32), (index * 2, 0, 0)))
            for packet in stream.encode(frame): container.mux(packet)
        for packet in stream.encode(): container.mux(packet)
    frames = extract_frames(path, max_frames=3)
    assert len(frames) == 3
    assert '29.667s' in frames[-1].name
    assert extract_frames(path, max_frames=0) == []


def test_failed_existing_source_is_not_deleted(tmp_path, db_session, monkeypatch):
    import app.db as db
    from app.models import KbDocument
    root = tmp_path/'docs'; root.mkdir()
    path = root/'a.md'; path.write_text('---\nscenario: vpn\n---\n# A\n\n## B\nold text')
    store = ChromaStore(str(tmp_path/'chroma'), 'preserve')
    monkeypatch.setattr(sync, 'SessionLocal', db.SessionLocal)
    monkeypatch.setattr(sync, 'embed_texts', lambda texts: [[1., 0.] for _ in texts])
    assert sync.run_sync(root, store, kb_name='integrity').added == ['a.md']
    before = store.get_all()
    path.write_text('invalid frontmatter')
    report = sync.run_sync(root, store, kb_name='integrity')
    assert report.failed and not report.deleted
    assert store.get_all() == before
    with db.SessionLocal() as session:
        assert session.query(KbDocument).filter_by(kb_name='integrity').one().status == 'active'


def test_update_removes_obsolete_chunks_only_after_success(tmp_path, monkeypatch):
    store = ChromaStore(str(tmp_path/'chroma'), 'obsolete')
    monkeypatch.setattr(sync, 'embed_texts', lambda texts: [[1., 0.] for _ in texts])
    doc = KnowledgeDoc('a.md', 'doc', {'scenario': 'vpn'}, '# A\n\n## One\nfirst\n\n## Two\nsecond')
    sync._sync_doc(doc, 'old', store, sync.SyncReport())
    old_ids = set(store.get_doc_ids('doc'))
    doc.content = '# A\n\n## One\nonly'
    sync._sync_doc(doc, 'new', store, sync.SyncReport())
    expected = {c.id for c in [sync.split_all([doc])[0].parent, *sync.split_all([doc])[0].children]}
    assert set(store.get_doc_ids('doc')) == expected
    assert old_ids - expected


def test_unchanged_document_repairs_missing_child(tmp_path, db_session, monkeypatch):
    import app.db as db
    root = tmp_path/'docs'; root.mkdir()
    (root/'a.md').write_text('---\nscenario: vpn\n---\n# A\n\n## B\ntext')
    store = ChromaStore(str(tmp_path/'chroma'), 'repair')
    monkeypatch.setattr(sync, 'SessionLocal', db.SessionLocal)
    monkeypatch.setattr(sync, 'embed_texts', lambda texts: [[1., 0.] for _ in texts])
    sync.run_sync(root, store, kb_name='repair')
    ids = set(store.get_all()); child = next(cid for cid in ids if not cid.endswith(':parent'))
    store.delete_ids([child])
    sync.run_sync(root, store, kb_name='repair')
    assert set(store.get_all()) == ids


@pytest.mark.parametrize('count,ready', [(0, True), (4, False)])
def test_switch_rejects_empty_or_failed_candidate(tmp_path, monkeypatch, count, ready):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api import kb
    monkeypatch.setattr(sync.settings, 'KB_VECTOR_DIR', str(tmp_path))
    monkeypatch.setattr(kb, 'create_store', lambda **kw: SimpleNamespace(
        count=lambda: count, is_build_ready=lambda: ready))
    with pytest.raises(HTTPException) as exc:
        kb.kb_switch(request=None, user=None)
    assert exc.value.status_code == 409
