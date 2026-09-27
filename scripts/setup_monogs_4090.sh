#!/usr/bin/env bash
# MonoGS 一键部署 —— RTX 4090 / torch 2.x（Route B 实测验证）
#
# 前置条件（镜像已满足，见 docs/monoGS_4090_image_checklist.md）:
#   - RTX 4090（Ada sm_89）+ 驱动 CUDA ≥ 11.8
#   - torch 2.x（实测 2.6.0+cu124 可用）+ CUDA 工具链 nvcc（实测 12.1）
#   - conda/miniconda
#
# 用法:
#   bash scripts/setup_monogs_4090.sh
#
# 可选环境变量:
#   CONDA_ENV=xxx        torch 所在 conda 环境（默认 myconda，Route B 镜像的环境名）
#   GIT_MIRROR=""        关闭 git 镜像（默认走 githubproxy.cc）
#   CUDA_HOME_DIR=...    nvcc 所在目录（默认 /usr/local/cuda-12.1）
#   RUN_SMOKE=1          装完自动跑 fr1_desk 冒烟（需先放好数据集）
#   WHEELS_DIR=...       若有本地 open3d 等 wheel，优先从该目录安装（省下载）
#
# 说明: 本脚本假定镜像已装好 torch 2.x；它负责「克隆仓库 → 装依赖(锁版本) →
#       打补丁 → 编译光栅化器 → 验证」。若镜像没装 torch，请先装 torch 2.x。
set -euo pipefail

# ==================== 可调参数 ====================
GIT_MIRROR_DEFAULT="https://githubproxy.cc/"
GIT_MIRROR="${GIT_MIRROR-$GIT_MIRROR_DEFAULT}"     # 用「-」而非「:-」，GIT_MIRROR="" 可关闭
REPO_URL="https://github.com/muskie82/MonoGS.git"
REPO_DIR="${MONOGS_DIR:-$HOME/MonoGS}"
CONDA_ENV="${CONDA_ENV:-myconda}"
CUDA_HOME_DIR="${CUDA_HOME_DIR:-/usr/local/cuda-12.1}"
TORCH_CUDA_ARCH="${TORCH_CUDA_ARCH:-8.9}"
SYSTEM_LIBSTDCXX="${SYSTEM_LIBSTDCXX:-/usr/lib/x86_64-linux-gnu/libstdc++.so.6}"
PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
RUN_SMOKE="${RUN_SMOKE:-0}"
WHEELS_DIR="${WHEELS_DIR:-}"

log() { echo -e "\n\033[1;32m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[提示] $*\033[0m"; }
die() { echo -e "\033[1;31m[错误] $*\033[0m" >&2; exit 1; }

# ==================== 1/8 GPU 检查 ====================
log "1/8 检查 GPU"
command -v nvidia-smi >/dev/null 2>&1 || die "未检测到 nvidia-smi"
nvidia-smi -L | head -2

# ==================== 2/8 pip 镜像 ====================
log "2/8 配置 pip 镜像 ($PIP_INDEX)"
PIP_HOST="$(echo "$PIP_INDEX" | sed -E 's#^https?://([^/:]+).*#\1#')"
mkdir -p ~/.pip ~/.config/pip
cat > ~/.pip/pip.conf <<EOF
[global]
index-url = $PIP_INDEX
trusted-host = $PIP_HOST
EOF
cp ~/.pip/pip.conf ~/.config/pip/pip.conf   # 有些镜像预置在 ~/.config，优先级更高

# ==================== 3/8 克隆仓库 ====================
log "3/8 克隆 MonoGS 仓库（含子模块）"
GIT_INSTEAD=()
if [ -n "$GIT_MIRROR" ]; then
  GIT_INSTEAD=(-c "url.${GIT_MIRROR}https://github.com/.insteadOf=https://github.com/")
  echo "    git 镜像: $GIT_MIRROR"
fi
if [ ! -d "$REPO_DIR" ]; then
  git "${GIT_INSTEAD[@]}" clone --progress "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR"
git "${GIT_INSTEAD[@]}" -c submodule.fetchJobs=8 submodule update --init --recursive
# 光栅化器内还嵌套了 glm 子模块，单独补一次
if [ -d submodules/diff-gaussian-rasterization ]; then
  git -C submodules/diff-gaussian-rasterization submodule update --init --recursive || true
fi

# ==================== 4/8 激活环境 + 验证 torch ====================
log "4/8 激活 conda 环境 ($CONDA_ENV) 并验证 torch"
# 定位 conda
if command -v conda >/dev/null 2>&1; then
  CONDA_BASE="$(conda info --base)"
else
  CONDA_BASE=""
  for d in "$HOME/miniconda3" "$HOME/anaconda3" "/opt/conda"; do
    [ -x "$d/bin/conda" ] && CONDA_BASE="$d" && break
  done
  [ -n "$CONDA_BASE" ] || die "未找到 conda"
fi
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV" 2>/dev/null || die "conda 环境 '$CONDA_ENV' 不存在（可用 CONDA_ENV=xxx 指定）"
python - <<'PY' || die "torch/CUDA 不可用，请确认镜像已装 torch 2.x + CUDA"
import torch
assert torch.cuda.is_available(), "torch.cuda.is_available() = False"
print("torch", torch.__version__, "| cuda", torch.version.cuda,
      "| device", torch.cuda.get_device_name(0))
PY

# ==================== 5/8 装依赖（锁版本）====================
log "5/8 安装依赖（版本锁定）"
# numpy 必须 < 2（MonoGS 用了 np.unicode_）
python -c "import numpy; assert int(numpy.__version__.split('.')[0]) < 2" 2>/dev/null \
  || pip install "numpy==1.26.4"
# evo 必须 1.11.0（1.37 删了 align_trajectory）
python -c "from evo.core import trajectory; assert hasattr(trajectory,'align_trajectory')" 2>/dev/null \
  || pip install "evo==1.11.0"

# open3d（400MB，最慢；已装或提供了本地 wheel 则跳过下载）
if python -c "import open3d" 2>/dev/null; then
  echo "    open3d 已装，跳过"
elif [ -n "$WHEELS_DIR" ] && ls "$WHEELS_DIR"/open3d-*.whl >/dev/null 2>&1; then
  pip install "$(ls "$WHEELS_DIR"/open3d-*.whl | head -1)"
else
  pip install "open3d==0.20.0"
fi

pip install munch plyfile trimesh tqdm lpips torchmetrics imgviz \
    PyOpenGL glfw PyGLM wandb rich ruff pyyaml
# opencv-python（若镜像已带 cv2 则跳过）
python -c "import cv2" 2>/dev/null || pip install opencv-python

# ==================== 6/8 打补丁 ====================
log "6/8 打补丁（headless GUI + 绘图容错）"
# 6a. 关闭 GUI（headless）
sed -i 's/^  use_gui: True$/  use_gui: False/' configs/mono/tum/base_config.yaml
grep -q "use_gui: False" configs/mono/tum/base_config.yaml || die "use_gui 补丁失败"
echo "    use_gui 已设为 False"

# 6b. eval_utils.py 绘图段包 try/except（幂等；matplotlib 3.9 与 evo 1.11 不兼容，绘图非必需）
if ! grep -q "plot skipped" utils/eval_utils.py; then
python - <<'PY'
path = "utils/eval_utils.py"
src = open(path).read()
old = '''    plot_mode = evo.tools.plot.PlotMode.xy
    fig = plt.figure()
    ax = evo.tools.plot.prepare_axis(fig, plot_mode)
    ax.set_title(f"ATE RMSE: {ape_stat}")
    evo.tools.plot.traj(ax, plot_mode, traj_ref, "--", "gray", "gt")
    evo.tools.plot.traj_colormap(
        ax,
        traj_est_aligned,
        ape_metric.error,
        plot_mode,
        min_map=ape_stats["min"],
        max_map=ape_stats["max"],
    )
    ax.legend()
    plt.savefig(os.path.join(plot_dir, "evo_2dplot_{}.png".format(str(label))), dpi=90)'''
new = '''    try:
        plot_mode = evo.tools.plot.PlotMode.xy
        fig = plt.figure()
        ax = evo.tools.plot.prepare_axis(fig, plot_mode)
        ax.set_title(f"ATE RMSE: {ape_stat}")
        evo.tools.plot.traj(ax, plot_mode, traj_ref, "--", "gray", "gt")
        evo.tools.plot.traj_colormap(
            ax,
            traj_est_aligned,
            ape_metric.error,
            plot_mode,
            min_map=ape_stats["min"],
            max_map=ape_stats["max"],
        )
        ax.legend()
        plt.savefig(os.path.join(plot_dir, "evo_2dplot_{}.png".format(str(label))), dpi=90)
    except Exception as e:
        Log(f"plot skipped: {e}", tag="Eval")'''
assert old in src, "eval_utils.py 绘图段未找到（版本可能已变化），请手动补丁"
open(path, "w").write(src.replace(old, new))
print("    eval_utils.py 已补丁")
PY
else
  echo "    eval_utils.py 已补丁过，跳过"
fi

# ==================== 7/8 编译 CUDA 光栅化器 ====================
log "7/8 编译 CUDA 光栅化器（nvcc=$CUDA_HOME_DIR, arch=$TORCH_CUDA_ARCH）"
export CUDA_HOME="$CUDA_HOME_DIR"
export PATH="$CUDA_HOME_DIR/bin:$PATH"
export TORCH_CUDA_ARCH_LIST="$TORCH_CUDA_ARCH"
pip install -e submodules/simple-knn
pip install -e submodules/diff-gaussian-rasterization
python -c "import diff_gaussian_rasterization, simple_knn; print('光栅化器 import OK')"

# ==================== 8/8 整体验证 ====================
log "8/8 整体验证"
LD_PRELOAD="$SYSTEM_LIBSTDCXX" python - <<'PY'
import torch, open3d, cv2, numpy, evo, munch, lpips, imgviz, trimesh, plyfile
print("numpy", numpy.__version__, "| evo", evo.__version__)
print("open3d", open3d.__version__, "| 全部关键依赖 import OK")
print("torch.cuda 可用:", torch.cuda.is_available())
PY

log "部署完成 ✅"
cat <<EOF
运行冒烟测试：
  source \$(conda info --base)/etc/profile.d/conda.sh && conda activate $CONDA_ENV
  cd $REPO_DIR
  export CUDA_HOME=$CUDA_HOME_DIR
  LD_PRELOAD=$SYSTEM_LIBSTDCXX python slam.py --config configs/mono/tum/fr1_desk.yaml
EOF

# ==================== 可选：冒烟测试 ====================
if [ "$RUN_SMOKE" = "1" ]; then
  log "运行冒烟测试（fr1_desk）"
  [ -d "datasets/tum/rgbd_dataset_freiburg1_desk" ] \
    || die "未找到 datasets/tum/rgbd_dataset_freiburg1_desk，请先放好数据集"
  LD_PRELOAD="$SYSTEM_LIBSTDCXX" python slam.py --config configs/mono/tum/fr1_desk.yaml
fi
