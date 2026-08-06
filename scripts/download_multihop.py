"""下载 MultiHopRAG 多跳评估数据集（HuggingFace，国内走 hf-mirror 镜像）。

用途：Phase 2/3 检索 Eval 的多跳基准（2556 条查询，证据分布 2~4 篇文档）。

用法：
    source .venv/bin/activate
    pip install datasets        # 首次（仅开发/测试用，不写入 requirements.txt）
    python -m scripts.download_multihop              # 下载全部
    python -m scripts.download_multihop --only corpus  # 只下文档库

输出：
    数据集/multihop-rag/multihop_rag.json    # 2556 条多跳查询
    数据集/multihop-rag/corpus.json          # 文档库（查询的 evidence 来源）
"""
import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path

# ===== 必须在 import datasets 之前设置环境变量（huggingface_hub import 时即读取）=====
# 国内网络默认走 hf-mirror 镜像（官方源被墙）。离线/内网可 --mirror 指定自己的源。
DEFAULT_MIRROR = "https://hf-mirror.com"

os.environ.setdefault("HF_ENDPOINT", DEFAULT_MIRROR)
# macOS 上 Python 自带 OpenSSL 不信任系统 Keychain 证书（SSL_CERT_VERIFY_FAILED），
# 用 certifi 的证书链兜底。certifi 是 datasets 的间接依赖，通常已随装。
try:
    import certifi

    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:  # 没有 certifi 时保持系统默认，报错会提示装
    pass

OUT_DIR = Path(__file__).resolve().parent.parent / "数据集" / "multihop-rag"


def _json_default(o):
    """JSON 序列化兜底：数据里含 datetime/date 字段（如广播日期）。"""
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def _dump(rows, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=_json_default)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror", default=None, help="HF 镜像地址（默认已走 hf-mirror）")
    parser.add_argument(
        "--only", choices=["queries", "corpus", "all"], default="all",
        help="只下载哪部分（默认 all）",
    )
    args = parser.parse_args()

    if args.mirror:
        os.environ["HF_ENDPOINT"] = args.mirror

    from datasets import load_dataset  # noqa: E402  # 延迟 import：确保环境变量先就位

    if args.only in ("queries", "all"):
        print(">>> 下载 MultiHopRAG 查询集（2556 条多跳查询）...")
        ds = load_dataset("yixuantt/MultiHopRAG", "MultiHopRAG")
        rows = [dict(r) for r in ds["train"]]
        _dump(rows, OUT_DIR / "multihop_rag.json")
        print(f"    完成：{len(rows)} 条查询 → {OUT_DIR / 'multihop_rag.json'}")

    if args.only in ("corpus", "all"):
        print(">>> 下载 corpus 文档库...")
        ds = load_dataset("yixuantt/MultiHopRAG", "corpus")
        rows = [dict(r) for r in ds["train"]]
        _dump(rows, OUT_DIR / "corpus.json")
        print(f"    完成：{len(rows)} 篇文档 → {OUT_DIR / 'corpus.json'}")

    print("\n字段示例（查询）：")
    if (OUT_DIR / "multihop_rag.json").exists():
        with open(OUT_DIR / "multihop_rag.json", encoding="utf-8") as f:
            sample = json.load(f)[0]
        print(json.dumps(sample, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
