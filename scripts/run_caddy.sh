#!/bin/sh
# Certbot是共享证书的唯一续期方。本服务检测公开证书变化后重载Caddy。
set -eu
server_pid=""
watcher_pid=""
stop_children() {
  [ -z "$watcher_pid" ] || kill "$watcher_pid" 2>/dev/null || true
  [ -z "$server_pid" ] || kill "$server_pid" 2>/dev/null || true
}
trap stop_children EXIT INT TERM
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
server_pid=$!
(
  certificate=/etc/letsencrypt/live/121.43.124.98/fullchain.pem
  previous=$(sha256sum "$certificate")
  while sleep 3600; do
    current=$(sha256sum "$certificate")
    if [ "$current" != "$previous" ]; then
      if caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force; then
        previous=$current
      else
        echo 'Certificate reload failed; retrying next hour' >&2
      fi
    fi
  done
) &
watcher_pid=$!
wait "$server_pid"
