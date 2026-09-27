#!/usr/bin/env bash
set -e
export DEBIAN_FRONTEND=noninteractive
# 重试 update（瞬时镜像同步失败会自动恢复）
for i in 1 2 3; do
  echo "==> apt update 第 $i 次"
  if apt-get update -qq 2>/tmp/apt_err.log; then echo "update OK"; break; fi
  echo "update 失败，等待 20s 重试..."; sleep 20
done
apt-get install -y -qq cmake libeigen3-dev libboost-all-dev libjsoncpp-dev libopengl-dev mesa-utils libglfw3-dev libglm-dev libopencv-dev libglew-dev libgl1-mesa-dev 2>&1 | tail -5
echo "APT_DEPS_DONE"
