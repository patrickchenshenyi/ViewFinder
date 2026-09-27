#!/usr/bin/env bash
source ~/miniconda3/etc/profile.d/conda.sh
conda activate myconda
cd ~/MonoGS
export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6
export CUDA_HOME=/usr/local/cuda-12.1
echo "=== 启动 slam.py (fr1_desk) ==="
python slam.py --config configs/mono/tum/fr1_desk.yaml
echo "SLAM_DONE exit=$?"
