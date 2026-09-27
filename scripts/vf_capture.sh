#!/usr/bin/env bash
# 采集控制（CLI 版，与网页「▶开始/⏹停止」等价）：
#   start  清空旧数据 + 起 ffmpeg + tum_mono 增量建图
#   stop   停 ffmpeg + DONE 哨兵（tum_mono 进入精修到 10000 迭代）
#   status 查看建图进度
# 用法：bash vf_capture.sh <start|stop|status>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/vf_env.sh"

case "${1:-}" in
  start)
    echo "→ 开始推流"
    vf_ssh 'bash ~/m4_start.sh'
    ;;
  stop)
    echo "→ 停止推流（进入精修）"
    vf_ssh 'bash ~/m4_stop.sh'
    ;;
  status)
    vf_ssh '
      echo "tum_mono: $(pgrep -f "bin/tum_mon[o]" >/dev/null && echo 运行中 || echo 未运行)"
      echo "ffmpeg:   $(pgrep -f "ffmpeg -nostdi[n]" >/dev/null && echo 运行中 || echo 未运行)"
      echo "拉流帧数: $(wc -l < ~/live/rgb.txt 2>/dev/null || echo 0)"
      echo "snapshot: $(ls -l --time-style=+%H:%M:%S ~/Photo-SLAM/results/m5_live/live/snapshot.ply 2>/dev/null | awk "{print \$6,\$7,\$5}" || echo 无)"
      echo "GPU: $(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader 2>/dev/null)"
    '
    ;;
  *)
    echo "用法: vf_capture.sh <start|stop|status>"; exit 1 ;;
esac
