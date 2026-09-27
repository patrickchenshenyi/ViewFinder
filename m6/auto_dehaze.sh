#!/bin/bash
# M6.0c 自动除雾：几何 prune（超大尺度/远距/低op大）+ F4 视图一致性 prune + 屏幕空间除雾（近相机淡雾层）→ dehazed.ply
# 用法: bash auto_dehaze.sh <final_ply> <cameras.json> <imgdir> <outdir>
set -e
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6
PY=/root/miniconda3/envs/myconda/bin/python
FINAL_PLY="$1"; CAMERAS="$2"; IMGDIR="$3"; OUTDIR="$4"
mkdir -p "$OUTDIR"
STEP1="$OUTDIR/dehaze_step1.ply"
STEP2="$OUTDIR/dehaze_step2.ply"

echo "[dehaze] 步骤1：几何 prune（prune_floaters.py）"
$PY /root/m6/prune_floaters.py --ply "$FINAL_PLY" --out "$STEP1" > /tmp/dehaze_step1.log 2>&1

echo "[dehaze] 步骤2：视图一致性 prune（prune_f4.py）"
$PY /root/m6/prune_f4.py --ply "$STEP1" --cameras "$CAMERAS" --out "$STEP2" > /tmp/dehaze_step2.log 2>&1

echo "[dehaze] 步骤3：屏幕空间除雾（compare_models.py --prune-fog）"
$PY /root/m6/compare_models.py --orig "$FINAL_PLY" --proc "$STEP2" \
    --cameras "$CAMERAS" --imgdir "$IMGDIR" --outdir "$OUTDIR/cmp" \
    --prune-fog --fog-out "$OUTDIR/dehazed.ply" > /tmp/dehaze_step3.log 2>&1

rm -f "$STEP1" "$STEP2"
echo "[dehaze] 完成：$OUTDIR/dehazed.ply"
