#!/usr/bin/env bash
# 本地（非 Docker）启动整套演练环境，供手工核对网页：
#   redis(需已在本机运行) + backend + proxy-a + proxy-b + web
# 默认读取可被环境变量覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${PYTHON:-python3}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
BACKEND_PORT="${BACKEND_PORT:-18100}"
PROXY_A_PORT="${PROXY_A_PORT:-18101}"
PROXY_B_PORT="${PROXY_B_PORT:-18102}"
WEB_PORT="${WEB_PORT:-18180}"
LOGDIR="${LOGDIR:-/tmp/diag-cb-logs}"
mkdir -p "$LOGDIR"

# 已有实例在运行则先停掉，保证读到的是最新代码
"$ROOT/tests/stop_local.sh" >/dev/null 2>&1 || true

start() { # name dir port env...
  local name="$1" dir="$2" port="$3"; shift 3
  ( cd "$ROOT/$dir" && env "$@" nohup "$PY" -m uvicorn app.main:app \
      --host 127.0.0.1 --port "$port" >"$LOGDIR/$name.log" 2>&1 &
    echo $! >"$LOGDIR/$name.pid" )
}

start backend backend "$BACKEND_PORT" BACKEND_HANG=2.0
for pair in "proxy-a:$PROXY_A_PORT" "proxy-b:$PROXY_B_PORT"; do
  name="${pair%%:*}"; port="${pair##*:}"
  start "$name" proxy "$port" PROXY_ID="$name" REDIS_URL="$REDIS_URL" \
    BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" BACKEND_TIMEOUT=2.5
done
start web web "$WEB_PORT" \
  PROXY_A_URL="http://127.0.0.1:$PROXY_A_PORT" \
  PROXY_B_URL="http://127.0.0.1:$PROXY_B_PORT" \
  BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"

echo "等待服务就绪…"
for port in "$BACKEND_PORT" "$PROXY_A_PORT" "$PROXY_B_PORT" "$WEB_PORT"; do
  ok=0
  for _ in $(seq 1 50); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then ok=1; break; fi
    sleep 0.2
  done
  [ "$ok" = 1 ] || { echo "端口 $port 未就绪，查看 $LOGDIR"; exit 1; }
done
echo "网页:  http://127.0.0.1:$WEB_PORT"
echo "代理A: http://127.0.0.1:$PROXY_A_PORT  代理B: http://127.0.0.1:$PROXY_B_PORT"
echo "日志:  $LOGDIR"
echo "停止:  tests/stop_local.sh"
