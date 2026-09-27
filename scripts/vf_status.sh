#!/usr/bin/env bash
# 全链路状态体检：隧道 + 云服务 + 手机 + 建图进程。
# 用法：bash vf_status.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/vf_env.sh"

echo "===== Mac 侧 ====="
echo "反隧道进程数: $(pgrep -f "reverse_tunnel[.]sh" | wc -l | tr -d ' ')"
if nc -z -G 2 127.0.0.1 8765 2>/dev/null; then echo "正隧道 8765: OK"; else echo "正隧道 8765: 未监听"; fi
echo "手机直连: $(curl -s -m 6 -o /dev/null -w 'HTTP %{http_code}' "http://$VF_PHONE/" 2>/dev/null || echo 不通)"

echo "===== 云侧 ====="
vf_ssh '
echo "node: $(pgrep -f "node serve[r].js" >/dev/null && echo 运行中 || echo 未运行)"
echo "tum_mono: $(pgrep -f "bin/tum_mon[o]" >/dev/null && echo 运行中 || echo 未运行)"
echo "ffmpeg: $(pgrep -f "ffmpeg -nostdi[n]" >/dev/null && echo 运行中 || echo 未运行)"
echo "云→手机(18080): $(curl -s -m 8 -o /dev/null -w "HTTP %{http_code}" http://127.0.0.1:18080/ || echo 不通)"
echo "GPU: $(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader 2>/dev/null)"
echo "拉流帧数: $(wc -l < ~/live/rgb.txt 2>/dev/null || echo 0)"
echo "snapshot.ply: $(ls -l --time-style=+%H:%M:%S ~/Photo-SLAM/results/m5_live/live/snapshot.ply 2>/dev/null | awk "{print \$6, \$7, \$5}")"
echo "cases: $(curl -s -m 6 http://127.0.0.1:8765/cases)"
'
