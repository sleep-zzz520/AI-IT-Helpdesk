#!/usr/bin/env bash
# 知识库向量库备份脚本（ROADMAP P3.3）
# 用途：打包 backend/.rag/（ChromaDB 向量库 + 蓝绿指针 + embedding 缓存）到 backups/kb/
#
# 重要说明：
#   - 【台账不在这里】知识库的文档列表/变更历史/过期标记在 MySQL（it_helpdesk 的 kb_* 表），
#     必须配合 scripts/backup_mysql.sh 一起备份才算完整。
#   - 【向量库可重建】.rag/ 本质是 docs/knowledge 的检索索引，丢了可从源文档重跑 sync 重建。
#     本脚本备份它只是为了「免重建」的加速，不是数据唯一副本。
#   - 容器部署：向量库在 backend 镜像内（未挂卷），重建容器会丢，需走 sync 重建；
#     本地部署跑本脚本即可持久化。
# 用法：bash scripts/backup_kb.sh
set -euo pipefail
cd "$(dirname "$0")/.."

KB_DIR="backend/.rag"
BACKUP_DIR="backups/kb"

if [[ ! -d "$KB_DIR" ]]; then
  echo "✗ 未找到向量库目录 $KB_DIR（还没 sync 过？先跑 python -m app.rag.sync）" >&2
  exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/kb_rag_${TS}.tar.gz"

echo "==> 备份向量库 $KB_DIR → $OUT"
tar -czf "$OUT" -C backend .rag
echo "✅ 备份完成: $OUT ($(du -h "$OUT" | cut -f1))"

# 清理：保留最近 7 份
ls -1t "$BACKUP_DIR"/kb_rag_*.tar.gz 2>/dev/null | tail -n +8 | while read -r old; do
  echo "  清理旧备份: $old"
  rm -f "$old"
done
echo "✅ 向量库备份完成（台账请另行备份 MySQL，见 backup_mysql.sh）"
