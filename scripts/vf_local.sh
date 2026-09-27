#!/usr/bin/env bash
# 本地视频直跑诊断：上传视频到云 → 抽帧重建 → 打印位姿覆盖摘要（判断是否取到正/侧/背/俯）。
# 用法：bash vf_local.sh <本地视频.mp4> [fps=10]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/vf_env.sh"

VIDEO="${1:-}"; FPS="${2:-10}"
[ -n "$VIDEO" ] && [ -f "$VIDEO" ] || { echo "用法: vf_local.sh <视频.mp4> [fps]"; exit 1; }
NAME="$(basename "$VIDEO")"

echo "→ 上传 $NAME ..."
vf_scp "$VIDEO" "$VF_USER@$VF_HOST:/root/m6/$NAME"

echo "→ 云端重建（后台），日志 /tmp/run_local.log ..."
vf_ssh "nohup setsid bash ~/m6/run_local_video.sh ~/m6/$NAME $FPS 1280:720 > /tmp/run_local.log 2>&1 < /dev/null & echo started"

# 轮询直到完成（最多 ~10 分钟）
for _ in $(seq 1 40); do
  sleep 15
  if vf_ssh 'grep -q "完成。结果目录" /tmp/run_local.log 2>/dev/null'; then
    echo "→ 完成："
    vf_ssh 'sed -n "/位姿覆盖摘要/,\$p" /tmp/run_local.log'
    exit 0
  fi
done
echo "→ 超时未完成，查看 /tmp/run_local.log 末尾："
vf_ssh 'tail -20 /tmp/run_local.log'
