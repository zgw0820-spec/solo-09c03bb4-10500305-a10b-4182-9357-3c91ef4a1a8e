#!/usr/bin/env bash
# 停止 tests/start_local.sh 拉起的本地进程。
set -uo pipefail
LOGDIR="${LOGDIR:-/tmp/diag-cb-logs}"
for pidfile in "$LOGDIR"/*.pid; do
  [ -e "$pidfile" ] || continue
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    echo "已停止 $(basename "$pidfile" .pid) (pid $pid)"
  fi
  rm -f "$pidfile"
done
