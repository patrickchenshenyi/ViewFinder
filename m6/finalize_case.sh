#!/bin/bash
# 归档「已完成 M0~M5」的 case：原始关键结果 + 自动除雾，写 manifest（时间戳区分）。
# 由 WS 服务在 tum_mono 退出（完整迭代完成）时调用。
set -e
# 可选参数：结果目录（默认实时链路 m5_live；本地视频直跑时传入其 results 目录）
RESULT_DIR="${1:-$HOME/Photo-SLAM/results/m5_live}"
CASES_DIR="$HOME/Photo-SLAM/results/cases"

FINAL_PLY=$(find "$RESULT_DIR" -name point_cloud.ply 2>/dev/null | grep shutdown | head -1)
if [ -z "$FINAL_PLY" ]; then
  echo "[finalize] 无最终 ply（未完成），丢弃，不归档"
  exit 1
fi

TS=$(date +%Y%m%d_%H%M%S)
CASE="$CASES_DIR/$TS"
mkdir -p "$CASE/original"

echo "[finalize] 归档 case $TS → $CASE"
cp "$FINAL_PLY" "$CASE/original/point_cloud.ply"

CAM=$(find "$RESULT_DIR" -name cameras.json 2>/dev/null | head -1)
[ -n "$CAM" ] && cp "$CAM" "$CASE/original/cameras.json"
TRJ=$(find "$RESULT_DIR" -name 'KeyFrameTrajectory*.txt' 2>/dev/null | head -1)
[ -n "$TRJ" ] && cp "$TRJ" "$CASE/original/"
PS=$(find "$RESULT_DIR" -name psnr.txt 2>/dev/null | head -1)
[ -n "$PS" ] && cp "$PS" "$CASE/original/psnr.txt"
IN=$(find "$RESULT_DIR" -name input.ply 2>/dev/null | head -1)
[ -n "$IN" ] && cp "$IN" "$CASE/original/input.ply"

# image 目录（除雾门禁用）
SHUTDIR=$(echo "$FINAL_PLY" | sed 's#/ply/point_cloud/.*##')
IMGDIR="$SHUTDIR/image"

echo "{\"ts\":\"$TS\",\"original\":\"done\",\"dehaze\":\"running\"}" > "$CASE/manifest.json"

if bash "$HOME/m6/auto_dehaze.sh" "$FINAL_PLY" "$CAM" "$IMGDIR" "$CASE/dehazed"; then
  echo "{\"ts\":\"$TS\",\"original\":\"done\",\"dehaze\":\"done\",\"original_ply\":\"original/point_cloud.ply\",\"dehazed_ply\":\"dehazed/dehazed.ply\"}" > "$CASE/manifest.json"
else
  # 除雾失败（如 cameras.json 缺失）：标记 failed 而不是卡在 running，
  # 网页仍可看原始模型；下次「开始推流」会按未完成 case 直接丢弃。
  echo "{\"ts\":\"$TS\",\"original\":\"done\",\"dehaze\":\"failed\"}" > "$CASE/manifest.json"
  echo "[finalize] 除雾失败，case 保留原始模型（dehaze:failed）" >&2
fi
echo "FINALIZED $TS"
