"""在项目根目录运行：python scripts/export_knowledge.py，生成Docker所需Git快照。"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "backend"))

from app.rag.git_source import read_snapshot, write_export  # noqa: E402

snapshot = read_snapshot(root)
write_export(snapshot, root / "knowledge-source.json")
print(f"Exported {len(snapshot.docs)} documents at {snapshot.commit}")
