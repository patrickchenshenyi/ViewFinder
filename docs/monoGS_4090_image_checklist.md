# MonoGS 云镜像重建清单（RTX 4090 已验证）

> 本文是「重建矩池云镜像」的最终落档，结论来自 Route B 实例（Ubuntu 22.04 + PyTorch 2.6.0 + CUDA 12.1）**实测跑通** MonoGS 的完整过程。
> 配套文档：`docs/plan.md`（总体计划）、`docs/gs_slam_deployment.md`（选型与部署）。

---

## 1. 基础镜像要求（硬性）

| 项 | 要求 | 说明 |
|---|---|---|
| GPU | RTX 4090（Ada, sm_89 / compute cap 8.9） | 需要 CUDA ≥ 11.8；驱动 ≥ 525 |
| 系统 | Ubuntu 22.04 | 20.04 亦可 |
| 驱动 | ≥ 525（实测 595.91.07 正常） | 驱动级 CUDA 13.2，向后兼容 |
| CUDA 工具链 | **nvcc ≥ 11.8**（实测 12.1） | 编译光栅化器必需，不能只有驱动 |
| PyTorch | **2.x**（实测 2.6.0 可用） | 避开 1.x + cu11.6/11.7 |
| conda | miniconda / anaconda | 环境隔离 |

> ⚠️ 关键结论：**torch 2.6.0 能直接编译并运行 MonoGS**（官方 environment.yml 锁的 1.12.1+cu11.6 在 4090 上跑不了）。如果镜像预装的是 2.x，可直接用；若是 1.x，需覆盖为 2.x。

---

## 2. 必须预装的 pip 包（省下载的关键）

镜像里预装好这些，以后开实例就是「装好」状态：

```
# 编译后运行时必需
open3d==0.20.0          # 0.20 即可；需配合 §3 的 GLIBCXX 修复
opencv-python            # 镜像若已带 cv2 可跳过

# 其余依赖（注意版本）
numpy==1.26.4           # 必须 < 2（MonoGS 用了 np.unicode_）
evo==1.11.0             # 必须 1.11.0（1.37 删了 align_trajectory）
munch plyfile trimesh tqdm
lpips torchmetrics imgviz
PyOpenGL glfw PyGLM wandb rich ruff pyyaml
```

> 依赖总数约 60MB（不含 open3d 的 400MB、opencv 的 60MB）。国内源直连慢，能预装就预装。

---

## 3. 六个配置修正（照做零踩坑）

| # | 坑 | 现象 | 修正 |
|---|---|---|---|
| 1 | numpy 2.0 删了 `np.unicode_` | `AttributeError: np.unicode_ was removed` | 锁 `numpy==1.26.4` |
| 2 | evo 1.37 删了模块级 `align_trajectory` | `AttributeError: no attribute align_trajectory` | 锁 `evo==1.11.0` |
| 3 | open3d 0.20 需要 `GLIBCXX_3.4.30`（GCC12+），conda 环境 libstdc++ 只有 3.4.29 | `ImportError: GLIBCXX_3.4.30 not found` | 运行时 `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6`（或预装 `libstdcxx-ng`）|
| 4 | matplotlib 3.9 与 evo 1.11 的 `traj_colormap` 不兼容 | `ValueError: Unable to determine Axes ... Colorbar` | 改 `utils/eval_utils.py`，把绘图段包进 `try/except`（失败不致命，ATE 数值照常保存）|
| 5 | pip 源默认 pypi.org | 下载龟速 | 配阿里源 `mirrors.aliyun.com/pypi/simple/`（写入 `~/.config/pip/pip.conf` 和 `~/.pip/pip.conf`）|
| 6 | GUI 需要显示器 | 服务器 headless 报错 | config 里 `Results.use_gui: False` |

### 修正 4 的具体补丁（`utils/eval_utils.py`）

把 `evaluate_evo` 里的绘图段包一层：

```python
    try:
        plot_mode = evo.tools.plot.PlotMode.xy
        fig = plt.figure()
        ax = evo.tools.plot.prepare_axis(fig, plot_mode)
        ax.set_title(f"ATE RMSE: {ape_stat}")
        evo.tools.plot.traj(ax, plot_mode, traj_ref, "--", "gray", "gt")
        evo.tools.plot.traj_colormap(ax, traj_est_aligned, ape_metric.error,
                                     plot_mode, min_map=ape_stats["min"],
                                     max_map=ape_stats["max"])
        ax.legend()
        plt.savefig(os.path.join(plot_dir, "evo_2dplot_{}.png".format(str(label))), dpi=90)
    except Exception as e:
        Log(f"plot skipped: {e}", tag="Eval")
```

---

## 4. 两条非镜像项（每次都要做）

1. **git clone 走镜像**：国内直连 github 慢，用
   ```bash
   git config --global url."https://githubproxy.cc/https://github.com/".insteadOf "https://github.com/"
   git clone https://github.com/muskie82/MonoGS.git
   ```
   （原 `musketyr/gaussian-splatting-slam` 已失效，仓库现名 **muskie82/MonoGS**。）

2. **子模块要递归拉全**，且光栅化器里还嵌套了 glm：
   ```bash
   git -c submodule.fetchJobs=8 submodule update --init --recursive
   cd submodules/diff-gaussian-rasterization
   git submodule update --init --recursive   # 补 third_party/glm
   ```

---

## 5. 编译 + 运行命令（已验证）

```bash
source ~/miniconda3/etc/profile.d/conda.sh
conda activate <env>
cd ~/MonoGS
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
export TORCH_CUDA_ARCH_LIST="8.9"
pip install -e submodules/simple-knn
pip install -e submodules/diff-gaussian-rasterization

# 运行（headless，需 §3 修正 3 的 LD_PRELOAD）
LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python slam.py --config configs/mono/tum/fr1_desk.yaml
```

---

## 6. M1 冒烟测试结果（fr1_desk，已验证）

| 验收项 | 结果 |
|---|---|
| 光栅化器编译 | ✅ diff-gaussian-rasterization + simple-knn（torch 2.6.0）|
| 跑完一段不 OOM | ✅ GPU 峰值 9.4GB / 24GB |
| 重建产物 | ✅ `point_cloud/final/point_cloud.ply`（1.86MB）|
| 轨迹精度 | ✅ ATE RMSE 7mm → 3.5cm（单目正常漂移）|

> 退出时的 `CUDA error: invalid resource handle` + `Producer process has been terminated...` 是 multiprocessing 收尾竞态，无害，`MonoGS: Done.` + `exit=0` 即成功。

---

## 7. 数据集（冒烟测试用）

- 用 **fr1_desk**（344MB，~613 帧）即可，比 fr3_long_office_household（1.48GB）小 4 倍。
- 官方源 `vision.in.tum.de` 国内直连只有 ~4KB/s，**推荐本地 proxy 下载后 scp 上传**。
- 云端解压到 `~/MonoGS/datasets/tum/`（config 里 `dataset_path` 相对该目录）。
- 格式：`rgb/`（彩色图）+ `depth/` + `rgb.txt` + `depth.txt` + `groundtruth.txt`。
