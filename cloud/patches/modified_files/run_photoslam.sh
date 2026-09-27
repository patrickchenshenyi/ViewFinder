#!/usr/bin/env bash
# Photo-SLAM 运行包装脚本（在 Route B 云实例上运行）
# 1) 运行前清理残留 GPU 进程，避免显存被孤儿进程占满导致 OOM
# 2) 跑完自动杀进程，省去 Photo-SLAM 收尾 ~5 分钟的卡顿（结果写完即可杀）
#
# 用法: bash run_photoslam.sh <slam.yaml> <gaussian.yaml> <seq目录> <输出目录>
# 例:   bash run_photoslam.sh \
#         ./cfg/ORB_SLAM3/Monocular/TUM/phone_720p.yaml \
#         ./cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml \
#         ~/Downloads/seq \
#         ~/Photo-SLAM/results/phone_720p_smoke

set -u
cd ~/Photo-SLAM

BIN=./bin/tum_mono
VOCAB=./ORB-SLAM3/Vocabulary/ORBvoc.txt
SLAM_YAML="$1"; GAUSS_YAML="$2"; SEQ_DIR="$3"; OUT_DIR="$4"

echo "==> 清理残留 GPU 进程（防止显存 OOM）"
pkill -9 -x tum_mono 2>/dev/null || true
pkill -9 -f 'multiprocessing.spawn' 2>/dev/null || true
pkill -9 -f 'slam[.]py' 2>/dev/null || true
sleep 2
echo -n "    当前空闲显存: "; nvidia-smi --query-gpu=memory.free --format=csv,noheader

echo "==> 启动 Photo-SLAM"
mkdir -p "$OUT_DIR"
nohup "$BIN" "$VOCAB" "$SLAM_YAML" "$GAUSS_YAML" "$SEQ_DIR" "$OUT_DIR" no_viewer \
  > /tmp/photo_slam_run.log 2>&1 < /dev/null &
PID=$!
echo "    PID=$PID"

echo "==> 等待结果写出（点云 + 最后一份轨迹出现即完成）"
for i in $(seq 1 7200); do
  PLY=$(find "$OUT_DIR" -name point_cloud.ply -print -quit 2>/dev/null)
  if [ -f "$OUT_DIR/KeyFrameTrajectory_EuRoC.txt" ] && [ -n "$PLY" ]; then
    echo "==> 结果已写完，自动结束进程（省去收尾卡顿）"
    kill -9 "$PID" 2>/dev/null || true
    break
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "==> 进程已自行退出"
    break
  fi
  sleep 3
done

echo "==> 完成，结果目录: $OUT_DIR"
find "$OUT_DIR" -name point_cloud.ply
