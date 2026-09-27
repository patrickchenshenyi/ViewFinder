#!/usr/bin/env bash
set -e
source ~/miniconda3/etc/profile.d/conda.sh
conda activate myconda
cd ~/MonoGS
echo "=== 确保子模块就位 ==="
git -c submodule.fetchJobs=8 submodule update --init --recursive 2>&1 | tail -2
git submodule status
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
echo "=== 编译 diff-gaussian-rasterization (torch 2.6.0 试编) ==="
pip install -e submodules/diff-gaussian-rasterization 2>&1 | tail -25
echo "=== 编译 simple-knn ==="
pip install -e submodules/simple-knn 2>&1 | tail -10
echo "COMPILE_DONE"
