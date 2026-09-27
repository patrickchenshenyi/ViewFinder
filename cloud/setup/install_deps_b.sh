#!/usr/bin/env bash
set -e
source ~/miniconda3/etc/profile.d/conda.sh
conda activate myconda
pip install plyfile tqdm opencv-python munch trimesh evo open3d torchmetrics imgviz PyOpenGL glfw PyGLM wandb lpips rich ruff 2>&1 | tail -15
echo "DEPS_DONE"
