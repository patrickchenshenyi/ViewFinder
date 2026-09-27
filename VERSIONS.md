# VERSIONS.md — 版本锁定表

> ViewFinder 全链路各组件的**精确版本**，用于在全新云实例 / 新 Mac 上复现环境。
>
> - 重建步骤见 **`build_guide.md`**（本文件是它的 §7 的权威版本）
> - 机器可读的快照在 **`cloud/env/`**（`pip freeze` / `apt-mark` / `dpkg -l` / 工具版本）
> - 最后核对：**2026-09-28**，来源实例：矩池云 RTX 4090 实例（域名/端口已隐去，见 `.env`）

---

## 0. 一句话摘要

```
Ubuntu 22.04 · RTX 4090 / driver 580.126.20 · CUDA 12.1 (nvcc 12.1.105)
OpenCV 4.10.0+CUDA @ /usr/local · LibTorch 2.1.2 cxx11-abi @ /root/libtorch
Photo-SLAM @ f8bfb2f · MonoGS @ 6c9254c
Node 18.17.1 / npm 9.6.7 · ws 8.21.3 · ffmpeg 4.4.2
conda base 3.11.4 · conda myconda 3.12.4 (torch 2.6.0 / numpy 2.2.6 / open3d 0.20.0)
```

---

## 1. 系统与驱动（云实例）

| 项 | 锁定值 | 实测来源 |
|---|---|---|
| 操作系统 | Ubuntu 22.04 (Jammy) | `/etc/os-release` · `PRETTY_NAME` |
| 内核 | 5.15.0-161-generic | `uname -a` |
| GPU | NVIDIA GeForce RTX 4090（sm_89） | `nvidia-smi` |
| 驱动 | **580.126.20**（要求 ≥ 525） | `nvidia-smi --query-gpu=driver_version` |
| CUDA 工具链 | **12.1**（nvcc 12.1.105） | `/usr/local/cuda-12.1/bin/nvcc --version` |
| 磁盘 | 200 GB（最小建议 120 GB） | `df -h /` |
| 家目录 | **`/root`**（硬要求，见下） | `echo $HOME` |

> ⚠️ **`/root` 是硬性要求，不是偏好**：`m6/render_pose.py` 里 `sys.path.insert(0, "/root/MonoGS")`、
> `m6/*.sh` 里 `/root/miniconda3/envs/myconda/bin/python`、`m6/baseline_stats.py` 里 `/root/live`
> 全部是硬编码路径。家目录不是 `/root` 就会全线失败。

```bash
# 核对命令
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
/usr/local/cuda-12.1/bin/nvcc --version | tail -2
whoami; echo "HOME=$HOME"
```

---

## 2. 编译期依赖（云实例）

| 项 | 锁定值 | 位置 | 获取方式 |
|---|---|---|---|
| OpenCV | **4.10.0**（`WITH_CUDA=ON`, `CUDA_ARCH_BIN=8.9`, `WITH_IPP=OFF`） | `/usr/local` | 源码编译（`cloud/setup/build_opencv_cuda.sh`） |
| OpenCV（apt 兜底） | 4.5.4（Ubuntu 22.04 自带） | `/usr/lib` | `apt install libopencv-dev` |
| LibTorch | **2.1.2+cu121，`cxx11-abi`** | `/root/libtorch` | [官方 CDN](https://download.pytorch.org/libtorch/cu121/libtorch-cxx11-abi-shared-with-deps-2.1.2%2Bcu121.zip) |
| CMake / GCC | 系统自带（gcc 11.x） | — | apt |
| jsoncpp / boost / Eigen | 系统包（**新 ABI**） | `/usr` | apt |
| CUDA 架构 | **sm_89** | — | `CUDA_ARCHITECTURES "89"` / `TORCH_CUDA_ARCH_LIST=8.9` |

```bash
ls /usr/local/lib/cmake/opencv4/OpenCVConfig.cmake
ldconfig -p | grep -c opencv                      # 期望 ~138
ls /root/libtorch/share/cmake/Torch/TorchConfig.cmake
```

> 🔴 **两条红线**：
> 1. LibTorch 必须是 **cxx11-abi** 变体（默认版是旧 ABI，会导致整条依赖链重编，实测卡在 boost 走不通）。
> 2. OpenCV 编译**不要**加 `-DCMAKE_CXX_FLAGS="-D_GLIBCXX_USE_CXX11_ABI=0"`。
> `cloud/legacy/rebuild_abi.sh` 是那条死路的留档，**勿用**。

---

## 3. 源码基线

| 组件 | 仓库 | 锁定 commit | 说明 |
|---|---|---|---|
| **Photo-SLAM** | `HuajianUP/Photo-SLAM` | **`f8bfb2f`**（"Update README.md"） | 本仓库补丁的精确基线；`.gitmodules` 为空，ORB-SLAM3 已 vendored |
| **MonoGS** | `muskie82/MonoGS` | **`6c9254c`**（"readme"） | M6 渲染流水线的**硬依赖**（非可选冒烟） |
| ↳ diff-gaussian-rasterization | `rmurai0610/diff-gaussian-rasterization-w-pose` | **`43e21bf`** | MonoGS submodule（带 pose 的魔改版） |
| ↳ simple-knn | `bkerbl/simple-knn`（gitlab.inria.fr） | **`44f7642`** | MonoGS submodule |

**Photo-SLAM 改动**（7 个文件）：见 `cloud/patches/photoslam_source_patches.patch`
（sha256 `a014e95ac03873ad1b03a34f28cdc27a9f754ce67dd097cbf8c3b68420ad55c4`），
完整副本见 `cloud/patches/modified_files/`。

> 实例上的实际克隆地址是镜像 `https://githubproxy.cc/https://github.com/HuajianUP/Photo-SLAM.git`
> （网络受限环境用）。补丁的上下文行依赖 `f8bfb2f`，换基线会 apply 失败。

```bash
# 核对命令
cd /root/Photo-SLAM && git log --oneline -1        # f8bfb2f
cd /root/MonoGS    && git log --oneline -1 && git submodule status
```

---

## 4. 运行时 — conda 环境（M6 流水线 / MonoGS 渲染）

| 项 | 锁定值 |
|---|---|
| conda 根 | `/root/miniconda3` |
| **base** | Python **3.11.4** + numpy 2.0.0（`m6/run_local_video.sh` 用 base） |
| **`myconda`** | Python **3.12.4**（名称与路径不可变） |

**关键包**（环境共 211 个包，权威快照见 `cloud/env/myconda.requirements.txt`）:

| 包 | 版本 | 谁需要 |
|---|---|---|
| torch | **2.6.0** | MonoGS 渲染、`m6/*.py` |
| torchvision | 0.21.0 | — |
| numpy | 2.2.6 | 全部 m6 脚本 |
| open3d | 0.20.0 | 点云 / PLY 处理 |
| plyfile | 1.1.5 | 13 个 m6 脚本 |
| scipy | 1.14.0 | `candidate_gen.py` / `l1_score.py` |
| pillow | 10.3.0 | 图像 |
| ImageIO | 2.37.4 | 图像 |
| matplotlib | 3.9.0 | 统计图 |
| **pyiqa** | **0.1.16** | `l1_pyiqa.py`（L1 画质打分） |
| requests | 2.34.2 | VLM HTTP 调用 |
| trimesh | 5.1.0 | 网格 |
| diff_gaussian_rasterization | `-e` @ `43e21bf` | `m6/render_pose.py`、`prune_f4.py` |
| simple_knn | `-e` @ `44f7642` | MonoGS |

> 🔴 **MonoGS 自带的 `environment.yml` 是错的**：它 pin 了 `cudatoolkit=11.6` / `python=3.7.13` /
> `pytorch=1.12.1` / `open3d=0.17.0`，与实际 `myconda`（3.12.4 / 2.6.0 / 0.20.0）**完全不符**。
> **不要用它建环境**，用 `cloud/env/myconda.requirements.txt`。

```bash
# 核对命令
/root/miniconda3/bin/python -V                                  # 3.11.4
/root/miniconda3/envs/myconda/bin/python -V                     # 3.12.4
/root/miniconda3/envs/myconda/bin/python -c "
import torch, numpy, plyfile, scipy, PIL, open3d, pyiqa
print('torch', torch.__version__, 'cuda', torch.cuda.is_available())
print('numpy', numpy.__version__, 'open3d', open3d.__version__)"
```

---

## 5. 运行时 — Node / 下行服务

| 项 | 锁定值 | 位置 |
|---|---|---|
| Node.js | **v18.17.1** | `/usr/lib/node-v18.17.1-linux-x64/bin/node`（软链 `/usr/local/bin/node`） |
| npm | 9.6.7 | 同上 |
| `ws` | **8.21.3** | `/root/m4_ws/node_modules/ws` |
| 服务脚本 | `scripts/m4_ws_server.js` → `/root/m4_ws/server.js` | 由 `vf_deploy.sh` 同步 |
| delta 模块 | `scripts/m7_ws_delta.js` → `/root/m4_ws/m7_ws_delta.js` | 同上 |
| 端口 | 8765（云 WS+HTTP）/ 8099（Mac 查看器）/ 18080（云→手机反隧道） | — |
| 快照路径 | `/root/Photo-SLAM/results/m5_live/live/snapshot.ply` | node 启动参数 |

```bash
node -v && npm -v
cat /root/m4_ws/package.json
grep -m1 '"version"' /root/m4_ws/node_modules/ws/package.json
```

---

## 6. 运行时 — 其他 CLI

| 项 | 锁定值 |
|---|---|
| ffmpeg | **4.4.2-0ubuntu0.22.04.1**（`/usr/bin/ffmpeg`） |
| 系统 python3 | 3.10.12（**无** numpy/torch，勿用） |

---

## 7. 业务参数（相机 / 训练）

| 项 | 锁定值 | 来源 |
|---|---|---|
| 相机内参 `phone_1280x720.yaml` | **fx=892.400 fy=892.253 cx=632.984 cy=357.109** | 一加 15 出厂标定 × 16:9 居中裁剪，活体 session 实测验证（`scripts/verify_live_intrinsics.py`） |
| 畸变 k1..k5 | **0**（未标定，待棋盘格） | — |
| 分辨率 / 帧率 | 1280×720 @ 10–15 fps（横屏） | IP Webcam |
| `max_num_iterations` | **15000** | `photo_slam_config/tum_mono.yaml` |
| `opacity_reset_interval` | **3000** | 同上 |
| `densify_until_iter` / `prune_big_point_after_iter` | 15000 / 15000 | 同上 |
| `sh_degree` | **3** | 同上 |
| 高斯数上限 | 1–2 M | `docs/plan.md` |

> ⚠️ **内参对质量影响极大**：粗值 `fx=1280` → PSNR 19.33 dB；真值 `fx=892.4` → **25.97 dB**（+6.64 dB）。
> 见 `docs/android_camera_intrinsics.md`。

---

## 8. Mac 侧

| 项 | 要求 | 备注 |
|---|---|---|
| macOS | 26.x（arm64，实测） | — |
| Node.js | ≥ 18 | `tools/serve-splat-web.mjs` 是**零依赖** ESM 服务 |
| Python 3 | 3.x | `scripts/android_intrinsics.py`、`calib_chessboard.py` |
| adb | platform-tools | 读手机相机内参 |
| 查看器前端依赖 | `@mkkellogg/gaussian-splats-3d` ^0.4.7、`three` ^0.186.0 | 已 vendored 到 `dist/vendor/`；`node_modules` 仅开发需要 |
| Brush | v0.3.0（arm64） | 第三方，**可选**；sha256 `65b2631398c839be3c1d4d7160fe2326389dec87830aac0710985e6690a1048c` |

---

## 9. 版本快照文件（`cloud/env/`）

| 文件 | 内容 | 行数 |
|---|---|---|
| `myconda.requirements.txt` | `conda myconda` 的 `pip freeze` | 208 |
| `apt-manual.txt` | `apt-mark showmanual` | 254 |
| `dpkg-versions.txt` | 全量 `dpkg -l`（含版本） | 1460 |
| `tool-versions.txt` | nvcc / node / npm / ffmpeg / nvidia-smi / 三个 Python | 11 |
| `monogs-versions.txt` | MonoGS + 2 个 submodule commit | 3 |
| `photoslam-upstream.txt` | Photo-SLAM commit + remote | 3 |

**重新生成全部快照**（云实例上）：

```bash
cd /path/to/ViewFinder && source scripts/vf_env.sh
vf_ssh '/root/miniconda3/envs/myconda/bin/pip freeze' > cloud/env/myconda.requirements.txt
vf_ssh 'apt-mark showmanual | sort'                    > cloud/env/apt-manual.txt
vf_ssh 'dpkg -l | awk "{print \$2\"=\"\$3}" | sort'    > cloud/env/dpkg-versions.txt
vf_ssh '/usr/local/cuda-12.1/bin/nvcc --version | tail -2; \
        echo "node: $(node -v)"; echo "npm: $(npm -v)"; \
        ffmpeg -version | head -1; \
        nvidia-smi --query-gpu=name,driver_version --format=csv,noheader; \
        echo "system python3: $(python3 -V)"; \
        echo "conda base: $(/root/miniconda3/bin/python -V)"; \
        echo "conda myconda: $(/root/miniconda3/envs/myconda/bin/python -V)"' > cloud/env/tool-versions.txt
vf_ssh 'cd /root/MonoGS && git log --oneline -1 && git submodule status' > cloud/env/monogs-versions.txt
vf_ssh 'cd /root/Photo-SLAM && git log --oneline -1 && git remote -v'    > cloud/env/photoslam-upstream.txt
```

> ⚠️ `nvcc` 必须写全路径：非交互式 SSH 的 `PATH` 不含 CUDA，直接 `nvcc` 会 `command not found`（实测踩到过）。
