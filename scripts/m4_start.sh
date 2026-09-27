#!/bin/bash
# M4 控制：开始推流（清空 + 全新起 ffmpeg 拉流 + tum_mono 增量建图）
set -e
echo "[start] 停止旧的 ffmpeg / tum_mono"
pkill -9 -f "ffmpeg -nostdin" 2>/dev/null || true
pkill -9 -f "bin/tum_mono" 2>/dev/null || true
sleep 1

echo "[start] 清空 ~/live 与结果目录"
rm -rf ~/live ~/Photo-SLAM/results/m5_live
mkdir -p ~/live/rgb
printf "# color images\n# file\n# timestamp filename\n" > ~/live/rgb.txt

echo "[start] 启动 ffmpeg 拉流"
# 注意：m3_pull_rtsp.sh v2 已改为按真实到达速率写帧 + 真实 mtime 时间戳，
# 不再强制 fps=10（避免 dup 假帧 + 假时间戳）。如需封顶可设 CAP_FPS=10。
RTSP_URL="rtsp://127.0.0.1:18080/h264_ulaw.sdp" OUT_DIR=~/live \
  nohup setsid bash ~/m3_pull_rtsp.sh > /tmp/m3_pull.log 2>&1 < /dev/null &

echo "[start] 启动 tum_mono 增量建图"
cd ~/Photo-SLAM
nohup setsid ./bin/tum_mono \
  ./ORB-SLAM3/Vocabulary/ORBvoc.txt \
  ./cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml \
  ./cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml \
  ~/live ~/Photo-SLAM/results/m5_live no_viewer \
  > /tmp/m5_live.log 2>&1 < /dev/null &

echo "[start] 完成"
