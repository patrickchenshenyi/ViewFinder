#!/bin/bash
# 本地视频直跑诊断：<video> → 抽帧(TUM) → tum_mono → 相机位姿覆盖摘要
# 用途：排查「流式链路(3fps+卡顿+假时间戳)」 vs 「拍摄方式/内参」哪一个是跟踪丢失元凶。
# 用法：bash run_local_video.sh <video.mp4> [fps] [scale]
#   默认 fps=10（与 phone_1280x720.yaml 的 Camera.fps 一致）、scale=1280:720（与内参一致）
set -euo pipefail

VIDEO="${1:-}"
FPS="${2:-10}"
SCALE="${3:-1280:720}"
NAME="local_$(date +%Y%m%d_%H%M%S)"
WORK="$HOME/live_$NAME"
RESULT="$HOME/Photo-SLAM/results/$NAME"

[ -n "$VIDEO" ] && [ -f "$VIDEO" ] || { echo "[local] 视频不存在: ${VIDEO:-<未提供>}"; exit 1; }
command -v ffmpeg  >/dev/null || { echo "[local] 无 ffmpeg"; exit 1; }
command -v python3 >/dev/null || { echo "[local] 无 python3"; exit 1; }

rm -rf "$WORK"; mkdir -p "$WORK/rgb"
mkdir -p "$RESULT"
echo "[local] 视频=$VIDEO  fps=$FPS  scale=$SCALE"
echo "[local] 工作目录=$WORK  结果目录=$RESULT"

# 1. 抽帧。本地文件没有网络卡顿，fps 滤镜能精确抽到目标帧率。
ffmpeg -nostdin -i "$VIDEO" -vf "fps=$FPS,scale=$SCALE" -q:v 2 "$WORK/rgb/%06d.png"

# 2. 生成 rgb.txt（本地文件时间戳 = (序号-1)/fps 是准确的）
python3 - "$WORK" "$FPS" <<'PY'
import os, sys
d, fps = sys.argv[1], float(sys.argv[2])
rgb = os.path.join(d, "rgb")
files = sorted(f for f in os.listdir(rgb) if f.endswith(".png"))
with open(os.path.join(d, "rgb.txt"), "w") as w:
    w.write("# color images\n# file\n# timestamp filename\n")
    for f in files:
        num = int(f.split(".")[0])
        w.write(f"{(num-1)/fps:.6f} rgb/{f}\n")
print(f"[local] 抽帧 {len(files)} 张")
PY

# 3. 放 DONE 哨兵（本地视频无需增量等待，直接跑完即结束）
touch "$WORK/DONE"

# 4. 跑 tum_mono（显存碎片化缓解：小切块分配，避免大 tensor 碎片导致的 CUDA OOM）
echo "[local] 开始 tum_mono ..."
cd "$HOME/Photo-SLAM"
export PYTORCH_CUDA_ALLOC_CONF="max_split_size_mb:64"
./bin/tum_mono \
  ./ORB-SLAM3/Vocabulary/ORBvoc.txt \
  ./cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml \
  ./cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml \
  "$WORK" "$RESULT" no_viewer \
  > "/tmp/$NAME.log" 2>&1
echo "[local] tum_mono 结束，日志 /tmp/$NAME.log"

# 5. 相机位姿覆盖摘要（判断是否取到 正/侧/背/俯 各视角）
echo "[local] ===== 位姿覆盖摘要 ====="
# 摘要需 numpy，用 miniconda python（系统 python3 无 numpy）
PYTHON="${PYTHON:-/root/miniconda3/bin/python}"
"$PYTHON" - "$RESULT" <<'PY'
import sys, json, glob, os
import numpy as np
res = sys.argv[1]
cams = glob.glob(os.path.join(res, "*_shutdown", "ply", "cameras.json"))
if not cams:
    print("[local] 未找到 cameras.json（重建可能失败），请查看 /tmp/*.log 末尾")
    sys.exit(1)
d = json.load(open(cams[0]))
pos = np.array([c["position"] for c in d])
fwd = np.array([np.array(c["rotation"]).reshape(3,3)[:,2] for c in d])
fwd = fwd / np.linalg.norm(fwd, axis=1, keepdims=True)
mean_f = fwd.mean(0); mean_f /= np.linalg.norm(mean_f)
cos = (fwd * mean_f).sum(1)
cos = np.clip(cos, -1, 1)
print(f"[local] 训练视角数: {len(d)}")
print(f"[local] 位置范围 x[{pos[:,0].min():.2f},{pos[:,0].max():.2f}] "
      f"y[{pos[:,1].min():.2f},{pos[:,1].max():.2f}] z[{pos[:,2].min():.2f},{pos[:,2].max():.2f}]")
print(f"[local] 轨迹总长: {np.linalg.norm(np.diff(pos,axis=0),axis=1).sum():.2f} m")
print(f"[local] 光轴与平均朝向夹角: max {np.degrees(np.arccos(cos.min())):.1f}° "
      f"(>60° 说明转到了侧面, >120° 说明有背面)")
PY
echo "[local] 完成。结果目录 $RESULT"
