#!/usr/bin/env bash
# MySQL 恢复脚本（ROADMAP P3.3 恢复演练）
# 用途：把 backups/mysql/ 里的 .sql.gz 备份恢复到 MySQL。
# 注意：会【清空并重建】目标库，生产请先确认。
# 用法：
#   bash scripts/restore_mysql.sh                # 恢复最新一份
#   bash scripts/restore_mysql.sh backups/mysql/it_helpdesk_20260807_120000.sql.gz  # 指定份
set -euo pipefail
cd "$(dirname "$0")/.."

MYSQL_DB="${MYSQL_DB:-it_helpdesk}"
MYSQL_USER="${MYSQL_USER:-helpdesk}"
if [[ -z "${MYSQL_PASSWORD:-}" && -f .env ]]; then
  MYSQL_PASSWORD=$(grep -E '^MYSQL_PASSWORD=' .env | head -1 | cut -d= -f2-)
fi
if [[ -z "${MYSQL_PASSWORD:-}" ]]; then
  echo "✗ 未找到 MYSQL_PASSWORD（.env 里配，或用环境变量传入）" >&2
  exit 1
fi

# 确定要恢复的备份文件
if [[ -n "${1:-}" ]]; then
  SRC="$1"
else
  SRC="$(ls -1t backups/mysql/it_helpdesk_*.sql.gz 2>/dev/null | head -1)"
  if [[ -z "$SRC" ]]; then
    echo "✗ 没找到备份，先跑 bash scripts/backup_mysql.sh" >&2
    exit 1
  fi
fi
[[ -f "$SRC" ]] || { echo "✗ 备份文件不存在: $SRC" >&2; exit 1; }
echo "==> 使用备份: $SRC"

echo "!! 将清空并重建库 $MYSQL_DB，5 秒后开始（Ctrl-C 取消）"
sleep 5

echo "==> 清空并重建库结构"
docker compose exec -T mysql env MYSQL_PWD="$MYSQL_PASSWORD" \
  mysql -u"$MYSQL_USER" -e "DROP DATABASE IF EXISTS \`$MYSQL_DB\`; CREATE DATABASE \`$MYSQL_DB\` CHARACTER SET utf8mb4;"

echo "==> 导入备份数据"
gunzip -c "$SRC" | docker compose exec -T mysql env MYSQL_PWD="$MYSQL_PASSWORD" \
  mysql -u"$MYSQL_USER" "$MYSQL_DB"

echo "==> 校验表数量"
docker compose exec -T mysql env MYSQL_PWD="$MYSQL_PASSWORD" \
  mysql -u"$MYSQL_USER" "$MYSQL_DB" -N -e "SELECT COUNT(*) AS tables_cnt FROM information_schema.tables WHERE table_schema='$MYSQL_DB';"
echo "✅ 恢复完成"
