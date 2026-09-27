#!/usr/bin/env bash
# 把本地云侧脚本同步到云端（改完这些脚本后跑一次）。
# 用法：bash vf_deploy.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/vf_env.sh"

echo "→ 上传云侧脚本 ..."
# scripts/ 下的云侧脚本
vf_scp "$SCRIPT_DIR/m3_pull_rtsp.sh"  "$VF_USER@$VF_HOST:/root/m3_pull_rtsp.sh"
vf_scp "$SCRIPT_DIR/m4_start.sh"      "$VF_USER@$VF_HOST:/root/m4_start.sh"
vf_scp "$SCRIPT_DIR/m4_stop.sh"       "$VF_USER@$VF_HOST:/root/m4_stop.sh"
vf_scp "$SCRIPT_DIR/m4_ws_server.js"  "$VF_USER@$VF_HOST:/root/m4_ws/server.js"
vf_scp "$SCRIPT_DIR/m7_ws_delta.js"   "$VF_USER@$VF_HOST:/root/m4_ws/m7_ws_delta.js"

# m6/ 下所有 .sh 与 .py（除雾 + 本地视频 + M6.6 取景/构图流水线）
for f in "$VF_ROOT"/m6/*.sh "$VF_ROOT"/m6/*.py; do
  [ -f "$f" ] || continue
  vf_scp "$f" "$VF_USER@$VF_HOST:/root/m6/$(basename "$f")"
done

# 训练/相机配置（本地 photo_slam_config/ 是云端同名文件的源，改动后需同步）
vf_scp "$VF_ROOT/photo_slam_config/tum_mono.yaml" \
       "$VF_USER@$VF_HOST:/root/Photo-SLAM/cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml"
vf_scp "$VF_ROOT/photo_slam_config/phone_1280x720.yaml" \
       "$VF_USER@$VF_HOST:/root/Photo-SLAM/cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml"

echo "→ 完成。"
echo "  提示：server.js 改动需重启 node 才生效（跑 vf_up.sh 或手动重启）。"
echo "  提示：~/m6/.env（DEEPSEEK_KEY）是密钥，不随本脚本同步，首次需手动写入。"
