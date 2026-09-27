#!/usr/bin/env bash
set -e
source ~/miniconda3/etc/profile.d/conda.sh
conda activate myconda
pip install munch plyfile evo trimesh lpips torchmetrics imgviz PyOpenGL glfw PyGLM wandb rich ruff pyyaml
echo "SMALL_DEPS_DONE"
