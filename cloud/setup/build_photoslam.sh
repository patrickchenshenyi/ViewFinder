#!/usr/bin/env bash
set -e
cd ~/Photo-SLAM
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
echo "=== 开始 build.sh ==="
bash build.sh
echo "BUILD_DONE"
