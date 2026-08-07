#!/usr/bin/env bash
# MySQL 备份脚本（ROADMAP P3.3）
# 用途：mysqldump 全量备份 it_helpdesk 库到 backups/mysql/，保留最近 KEEP 份。
# 说明：
#   - 备份的是「不可重建」数据：工单/会话/审计/用户/知识库台账（向量库可从文档重建，见 backup_kb.sh）
#   - 用 docker compose exec 而非具体容器名（不依赖前缀，改名也能跑）
# 用法：bash scripts/backup_mysql.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# ===== 配置（可用环境变量覆盖）=====
BACKUP_DIR="${BACKUP_DIR:-backups/mysql}"
KEEP="${KEEP:-7}"                                  # 保留最近 7 份
MYSQL_DB="${MYSQL_DB:-it_helpdesk}"
MYSQL_USER="${MYSQL_USER:-helpdesk}"
# mysqldump 密码：从 .env 读（避免明文写脚本里）；没 .env 则用环境变量 MYSQL_PASSWORD
if [[ -z "${MYSQL_PASSWORD:-}" && -f .env ]]; then
  MYSQL_PASSWORD=$(grep -E '^MYSQL_PASSWORD=' .env | head -1 | cut -d= -f2-)
fi
if [[ -z "${MYSQL_PASSWORD:-}" ]]; then
  echo "✗ 未找到 MYSQL_PASSWORD（.env 里配，或用环境变量传入）" >&2
  exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/it_helpdesk_${TS}.sql.gz"

echo "==> 备份 MySQL 库 $MYSQL_DB → $OUT"
# mysqldump 在 mysql 容器内；--single-transaction 不锁表，--routines 带存储过程
# 密码通过 MYSQL_PWD 传入，避免命令行暴露（ps 可见）
docker compose exec -T mysql \
  env MYSQL_PWD="$MYSQL_PASSWORD" \
  mysqldump -u"$MYSQL_USER" --single-transaction --routines --hex-blob "$MYSQL_DB" \
  | gzip > "$OUT"

# 校验非空
if [[ ! -s "$OUT" ]]; then
  echo "✗ 备份文件为空，失败" >&2
  exit 1
fi
echo "✅ 备份完成: $OUT ($(du -h "$OUT" | cut -f1))"

# 清理：保留最近 KEEP 份
ls -1t "$BACKUP_DIR"/it_helpdesk_*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  echo "  清理旧备份: $old"
  rm -f "$old"
done
echo "✅ 保留最近 $KEEP 份备份于 $BACKUP_DIR/"
