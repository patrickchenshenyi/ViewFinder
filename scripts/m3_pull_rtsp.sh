#!/usr/bin/env bash
# M3: 云实例端拉取手机 IP Webcam 的 RTSP 流，持续抽帧成 TUM 序列。
# 在云实例（Ubuntu）上运行，需要已装 ffmpeg、python3。
#
# 用法（通过环境变量覆盖默认值）：
#   RTSP_URL="rtsp://127.0.0.1:18080/h264_ulaw.sdp" OUT_DIR=~/live bash m3_pull_rtsp.sh
#
# v2 修复（假时间戳 bug）：
#   1) 不再用 `fps=N` 滤镜强制 10fps（输入不足时会 dup 假帧，且让帧率看起来像 10fps）；
#      改为 `-vsync 0` passthrough，按真实到达速率写帧。
#   2) rgb.txt 时间戳不再用 `(序号-1)/fps` 伪造，改为「文件真实 mtime 相对首帧」。
#      这样 SLAM 拿到的是真实到达时刻（含网络卡顿），运动模型不再被压缩时间轴误导。
#   可选 CAP_FPS 封顶（若手机端帧率过高需要抽稀时用），SCALE 按需缩放。

set -euo pipefail

RTSP_URL="${RTSP_URL:-rtsp://127.0.0.1:18080/h264_ulaw.sdp}"
SCALE="${SCALE:-}"                        # 可选，如 "scale=1280:720"；留空则不缩放
CAP_FPS="${CAP_FPS:-}"                    # 可选封顶帧率，如 10；留空=按到达原速写帧
OUT_DIR="${OUT_DIR:-$HOME/live}"
RGB_DIR="$OUT_DIR/rgb"
RGB_TXT="$OUT_DIR/rgb.txt"

command -v ffmpeg  >/dev/null || { echo "ffmpeg not found"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
mkdir -p "$RGB_DIR"

# 续传：从已有最大序号 +1 开始，避免重启 ffmpeg 覆盖旧帧
NEXT=1
last=$(ls "$RGB_DIR"/*.png 2>/dev/null | sed 's#.*/##' | grep -oE '^[0-9]+' | sort -n | tail -1 || true)
[ -n "$last" ] && NEXT=$((10#$last + 1))

# 组装滤镜：先缩放，再（可选）封顶帧率
VF=""
[ -n "$SCALE" ] && VF="$SCALE"
[ -n "$CAP_FPS" ] && { [ -n "$VF" ] && VF="$VF,"; VF="${VF}fps=$CAP_FPS"; }

echo "[m3] src=$RTSP_URL scale=${SCALE:-none} cap_fps=${CAP_FPS:-none} out=$RGB_DIR start=$NEXT"

# 持续拉流抽帧（后台）。-vsync 0 = passthrough：不 dup 不 drop，按到达写帧。
ffmpeg -nostdin \
  -rtsp_transport tcp \
  -fflags nobuffer \
  -stimeout 5000000 \
  -i "$RTSP_URL" \
  -vsync 0 \
  ${VF:+-vf "$VF"} \
  -q:v 2 \
  -start_number "$NEXT" \
  "$RGB_DIR/%06d.png" &
FFMPEG_PID=$!
# INT/TERM 时先杀 ffmpeg 再退出（否则脚本会吞掉信号、无限空转、只能 kill -9）
trap 'kill $FFMPEG_PID 2>/dev/null || true; exit 0' INT TERM
trap 'kill $FFMPEG_PID 2>/dev/null || true' EXIT

# 循环重建 rgb.txt：时间戳 = 文件真实 mtime（相对首帧），跳过正在写入的最后一帧。
regenerate() {
python3 - "$RGB_DIR" <<'PY'
import os, sys
d = sys.argv[1]
files = sorted((f for f in os.listdir(d) if f.endswith('.png')),
               key=lambda f: int(f.split('.')[0]))
if files:
    files = files[:-1]  # 跳过正在写入的最后一帧
mts = [os.path.getmtime(os.path.join(d, f)) for f in files]
t0 = mts[0] if mts else 0.0
print("# color images"); print("# file"); print("# timestamp filename")
for f, mt in zip(files, mts):
    print(f"{mt - t0:.6f} rgb/{f}")
PY
}

echo "[m3] regenerating rgb.txt every 2s (Ctrl-C to stop)..."
while true; do
  regenerate > "$RGB_TXT"
  sleep 2
done
