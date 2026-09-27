#!/bin/bash
# M4 控制：停止推流（停 ffmpeg + 放 DONE 哨兵，tum_mono 结束输入进入精修）
echo "[stop] 停止 ffmpeg 拉流"
pkill -9 -f "ffmpeg -nostdin" 2>/dev/null || true
pkill -f "m3_pull_rtsp.sh" 2>/dev/null || true
sleep 1

echo "[stop] 放置 DONE 哨兵"
touch ~/live/DONE
echo "[stop] 完成（tum_mono 将继续精修到 10000 迭代）"
