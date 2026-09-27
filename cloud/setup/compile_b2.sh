#!/usr/bin/env bash
set -e
source ~/miniconda3/etc/profile.d/conda.sh
conda activate myconda
cd ~/MonoGS
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
export TORCH_CUDA_ARCH_LIST="8.9"
echo "=== 编译 diff-gaussian-rasterization ==="
pip install -e submodules/diff-gaussian-rasterization 2>&1 | tail -30
echo "=== 编译 simple-knn ==="
pip install -e submodules/simple-knn 2>&1 | tail -10
echo "COMPILE_DONE"
