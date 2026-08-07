"""Embedding 输入清洗测试（离线，不触发真实 API）。

回归覆盖：Eval 3/4（qa_eval）真实跑时，LLM 反推的问题偶发异常内容
（空串/超长/控制字符）导致智谱 embedding 返回 400（code 1210）崩整个流程。
修复：embed_texts 清洗空文本返回零向量、超长截断、剥离控制字符。
本测试只验证清洗逻辑，不调 embedding API。
"""
from app.rag.embedder import EMBED_DIM, _clean_text, embed_texts


def test_strip_control_chars():
    """控制字符剥离：不送 API，避免 400。"""
    assert _clean_text("a\x00b\x01c") == "abc"


def test_truncate_overlong():
    """超长文本截断到安全长度。"""
    long = "x" * 5000
    assert len(_clean_text(long)) <= 1800


def test_clean_preserves_normal():
    """正常文本不受影响。"""
    assert _clean_text("VPN 客户端配置步骤") == "VPN 客户端配置步骤"


def test_empty_text_zero_vector(monkeypatch):
    """空文本不送 API、返回零向量；正常文本才走真实批次（mock 掉 _embed_batch）。

    关键：mock `_embed_batch` 避免触发真实 embedding API（CI 无有效 key 会 401）。
    验证的是【清洗分发逻辑】——空文本被拦截、只有非空文本送批次。
    """
    import app.rag.embedder as m

    # mock 掉真实批次：只记录它收到了哪些文本，返回假向量
    captured = []

    def fake_batch(texts):
        captured.extend(texts)
        return [[i + 1] * EMBED_DIM for i in range(len(texts))]

    monkeypatch.setattr(m, "_embed_batch", fake_batch)

    vecs = embed_texts(["", "正常文本", "  ", "另一段"])

    assert len(vecs) == 4
    assert vecs[0] == [0.0] * EMBED_DIM   # 空文本 → 零向量
    assert vecs[2] == [0.0] * EMBED_DIM   # 纯空白 → 零向量
    assert captured == ["正常文本", "另一段"], \
        f"只有非空文本送批次，实际: {captured}"  # 空/空白不送 API


def test_all_empty_batch(monkeypatch):
    """全空批次不调 API（_embed_batch 不被调用），全部零向量。"""
    import app.rag.embedder as m

    called = []

    def fake_batch(texts):
        called.append(texts)
        return []

    monkeypatch.setattr(m, "_embed_batch", fake_batch)

    vecs = embed_texts(["", ""])
    assert vecs == [[0.0] * EMBED_DIM, [0.0] * EMBED_DIM]
    assert called == [], "全空批次不应调用真实 embedding API"
