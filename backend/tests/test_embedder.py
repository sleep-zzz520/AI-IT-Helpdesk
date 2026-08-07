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


def test_empty_text_zero_vector():
    """空文本返回零向量，不抛错（之前会崩整个流程）。"""
    vecs = embed_texts(["", "正常文本"])
    assert len(vecs) == 2
    assert vecs[0] == [0.0] * EMBED_DIM  # 空文本 → 零向量
    assert len(vecs[1]) == EMBED_DIM     # 正常文本占位（长度正确）


def test_all_empty_batch():
    """全空批次不调 API，全部零向量。"""
    vecs = embed_texts(["", ""])
    assert vecs == [[0.0] * EMBED_DIM, [0.0] * EMBED_DIM]
