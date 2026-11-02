#!/usr/bin/env bash
# 仅将停写知识备份恢复至随机隔离schema；不停止/改写生产服务。
set -euo pipefail
cd "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

exec 9>backups/.retention.lock
python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)'
prefix="$(cat backups/latest-kb-backup)"
if [[ ! "$prefix" =~ ^pre-sync-[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo 'Invalid knowledge backup prefix.' >&2
  exit 1
fi
for suffix in knowledge.sql rag.tar.gz manifest.json; do
  test -f "backups/$prefix-$suffix"
  test -s "backups/$prefix-$suffix"
done
# 标准库预检不连接数据库，拒绝跨schema或两张知识表之外的写入。
PYTHONPATH=backend python3 -c 'import sys; from app.rag.backup_restore_check import validate_knowledge_dump; validate_knowledge_dump(sys.argv[1])' \
  "backups/$prefix-knowledge.sql" </dev/null
clone="helpdesk_restore_$(python3 -c 'import secrets; print(secrets.token_hex(16))' </dev/null)"
readonly clone
if [[ ! "$clone" =~ ^helpdesk_restore_[a-f0-9]{32}$ ]]; then
  echo 'Invalid isolated restore database.' >&2
  exit 1
fi

clone_sql() {
  docker compose exec -T mysql sh -c '
    set -eu
    action="$1"
    clone="$2"
    case "$clone" in helpdesk_restore_*) ;; *) exit 2 ;; esac
    suffix="${clone#helpdesk_restore_}"
    case "$suffix" in ""|*[!a-f0-9]*) exit 2 ;; esac
    test "${#suffix}" -eq 32
    case "$MYSQL_USER" in ""|*[!A-Za-z0-9_]*) exit 2 ;; esac
    case "$action" in
      create)
        query="CREATE DATABASE \`$clone\` CHARACTER SET utf8mb4; GRANT ALL PRIVILEGES ON \`$clone\`.* TO '\''$MYSQL_USER'\''@'\''%'\'';"
        ;;
      cleanup)
        query="DROP DATABASE IF EXISTS \`$clone\`; REVOKE ALL PRIVILEGES ON \`$clone\`.* FROM '\''$MYSQL_USER'\''@'\''%'\'';"
        ;;
      *) exit 2 ;;
    esac
    MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --batch --skip-column-names -uroot -e "$query"
  ' sh "$1" "$clone" </dev/null
}
cleanup() {
  result=$?
  trap - EXIT
  if ! clone_sql cleanup; then
    echo 'Isolated restore schema/grant cleanup failed.' >&2
    result=1
  fi
  exit "$result"
}
trap cleanup EXIT
clone_sql create
# 只有此条命令读取SQL stdin，避免吞掉调用方的SSH脚本。
docker compose exec -T mysql sh -c '
  set -eu
  clone="$1"
  case "$clone" in helpdesk_restore_*) ;; *) exit 2 ;; esac
  suffix="${clone#helpdesk_restore_}"
  case "$suffix" in ""|*[!a-f0-9]*) exit 2 ;; esac
  test "${#suffix}" -eq 32
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot "$clone"
' sh "$clone" < "backups/$prefix-knowledge.sql"

docker compose run --rm --no-deps -T -e "MYSQL_DB=$clone" \
  -v "$PWD/backups:/backup:ro" backend python -m app.rag.backup_restore_check \
  --archive "/backup/$prefix-rag.tar.gz" --manifest "/backup/$prefix-manifest.json" \
  </dev/null > "backups/$prefix-restore-check.json"
cat "backups/$prefix-restore-check.json"
