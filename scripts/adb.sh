#!/usr/bin/env bash
# scripts/adb.sh —— 项目内置 adb 包装器
#
# 为什么需要它：
#   1) 这台 Mac 没装 platform-tools，已把官方包解压到 tools/platform-tools/；
#   2) DSH sandbox 不允许写 ~/.android，所以把 HOME 指到项目内 .adbhome/
#      （adb 只在这里放 adbkey / 日志，不影响系统其它东西）。
#
# 用法与 adb 完全一致：
#   bash scripts/adb.sh devices -l
#   bash scripts/adb.sh shell getprop ro.product.model
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADB="$ROOT/tools/platform-tools/adb"

if [ ! -x "$ADB" ]; then
  echo "[adb.sh] 缺少 $ADB" >&2
  echo "[adb.sh] 重新下载：" >&2
  echo "  curl -L -o tools/_downloads/platform-tools-darwin.zip \\" >&2
  echo "    https://mirrors.cloud.tencent.com/AndroidSDK/platform-tools_r35.0.2-darwin.zip" >&2
  echo "  unzip -q -o tools/_downloads/platform-tools-darwin.zip -d tools/" >&2
  exit 1
fi

export HOME="$ROOT/.adbhome"
export ANDROID_USER_HOME="$ROOT/.adbhome"
mkdir -p "$HOME"

exec "$ADB" "$@"
