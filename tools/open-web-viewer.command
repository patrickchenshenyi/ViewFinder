#!/bin/bash
# 双击即可：拉起本地查看器服务（若未运行）并打开浏览器。
# 也可在终端执行： ./open-web-viewer.command
set -u

cd "$(dirname "$0")" || exit 1

PORT="${PORT:-8099}"
URL="http://127.0.0.1:${PORT}/"

# Finder 启动时 PATH 可能不含 nvm 的 node，显式兜底
NODE_BIN="$(command -v node || true)"
if [ -z "$NODE_BIN" ]; then
  for cand in "$HOME/.nvm/versions/node"/*/bin/node /opt/homebrew/bin/node /usr/local/bin/node; do
    [ -x "$cand" ] && NODE_BIN="$cand" && break
  done
fi
if [ -z "$NODE_BIN" ]; then
  echo "找不到 node，请先安装 Node.js（brew install node）"
  exit 1
fi

if curl -s -m 2 -o /dev/null "${URL}__health"; then
  echo "服务已在运行：${URL}"
else
  echo "启动查看器服务…"
  nohup "$NODE_BIN" serve-splat-web.mjs >> splat-web/.server.log 2>&1 &
  for _ in $(seq 1 40); do
    sleep 0.25
    curl -s -m 1 -o /dev/null "${URL}__health" && break
  done
  if ! curl -s -m 2 -o /dev/null "${URL}__health"; then
    echo "启动失败，日志尾部："
    tail -20 splat-web/.server.log 2>/dev/null
    exit 1
  fi
  echo "服务已就绪：${URL}"
fi

open "$URL"
echo "已在浏览器打开 ${URL}"
echo "（关闭服务：pkill -f serve-splat-web.mjs）"
