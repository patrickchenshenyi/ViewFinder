# M2 Photo-SLAM 交接文档（供另一 DSH session 排查）

> 生成时间：2026-09-22。本文件记录当前 ViewFinder 项目的完整状态、已踩的坑、以及下一步。
> 项目总体计划见 `docs/plan.md`，部署选型见 `docs/gs_slam_deployment.md`。

---

## 0. 项目背景（一句话）

Android 手机 RGB 视频 → 云端 3D Gaussian Splatting 在线重建 → PC 浏览器展示（toy demo）。
当前在云 GPU 实例上搭 SLAM 重建环境。里程碑：
- **M1 = MonoGS**（环境冒烟，单目，纯验证）—— **已全部完成** ✅
- **M2 = Photo-SLAM**（接手机视频，带回环，主路径）—— **进行中，卡在 C++ 编译/链接** ⏳

---

## 1. 云实例与连接方式

### 当前唯一活跃实例（Route B）
- **SSH**：`ssh -p <实例端口> root@<实例域名>` —— 真值见仓库根目录 `.env`
  （2026-09-28：出于本仓库**公开**的原因，已从所有文档中隐去实例域名 / 端口 / 主机名）
- **密码**：**见 `.env` 的 `viewfinder_password`**（2026-09-28 已从本文件移除明文；
  仓库内任何文件都不得再写入密码，脚本统一走 `scripts/vf_env.sh` 从 `.env` 读取）
- **主机名**：`<已隐去>`
- **镜像**：Ubuntu 22.04 + Python 3.12 + PyTorch 2.6.0(cu124) + CUDA 12.1(nvcc) + VNC
- **GPU**：RTX 4090（24GB）
- **磁盘**：200GB；内存 64GB；CPU 12 核

### Route A（已释放，不再使用）
- 同为矩池云实例，已释放。无需再管。

### 本机沙箱如何 SSH（重要，另一个 session 要照做）
沙箱里 `expect` 的 `spawn` 会报 "no more ptys"，但 **SSH_ASKPASS 方式可用**：
```bash
cat > /tmp/askpass_b.sh <<'EOF'
#!/bin/sh
printf '%s\n' "$VF_PASS"   # 从 .env 读，勿写死明文（见 vf_env.sh）
EOF
chmod +x /tmp/askpass_b.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/askpass_b.sh SSH_ASKPASS_REQUIRE=force
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -p <实例端口> root@<实例域名> '<命令>'
```
> 更省事的做法：`source scripts/vf_env.sh && vf_ssh '<命令>'`（已封装 askpass 与端口）。

---

## 2. 已经完成的事（可复用成果）

### M1 MonoGS（Route B 上已跑通，100% 完成）
- MonoGS 仓库：`muskie82/MonoGS`（原 `musketyr/gaussian-splatting-slam` 已失效），clone 在 `~/MonoGS`。
- 环境：conda env `myconda`（镜像自带 torch 2.6.0+cu124）。依赖补齐并修复：
  - `numpy==1.26.4`（必须 <2，MonoGS 用了 `np.unicode_`）
  - `evo==1.11.0`（1.37 删了 `align_trajectory`）
  - `open3d==0.20.0`（运行时需 `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6` 解决 GLIBCXX_3.4.30）
  - `eval_utils.py` 绘图段包了 try/except（matplotlib 3.9 与 evo 1.11 不兼容）
- **验收结果**：`slam.py --config configs/mono/tum/fr1_desk.yaml` 完整跑通，产出
  `~/MonoGS/results/tum_rgbd_dataset_freiburg1_desk/<时间戳>/point_cloud/final/point_cloud.ply`，
  ATE 误差 7mm→3.5cm，GPU 峰值 9.4GB/24GB，无 OOM。`MonoGS: Done.` exit=0。
- 数据集 fr1_desk：本地已下载 `/tmp/tum_download/rgbd_dataset_freiburg1_desk.tgz`（344MB），
  已上传解压到 Route B 的 `~/MonoGS/datasets/tum/rgbd_dataset_freiburg1_desk/`。

### 已产出的本地文档/脚本（仓库根目录）
- `docs/monoGS_4090_image_checklist.md` —— 镜像重建清单（M1 完整结论）
- `scripts/setup_monogs_4090.sh` —— MonoGS 一键部署（已验证，语法通过）
- `docs/plan.md`、`docs/gs_slam_deployment.md` —— 原有计划（部分内容已过时，见下）

### M2 Photo-SLAM 已完成的部分
- 仓库 `HuajianUP/Photo-SLAM` clone 在 `~/Photo-SLAM`（官方 HUANG-ZHIHAO 未试，用 fork）。
- 系统依赖 apt 已装：cmake、libeigen3-dev、libboost-all-dev、libjsoncpp-dev、libopengl-dev、
  mesa-utils、libglfw3-dev、libglm-dev、libopencv-dev（CPU 4.5.4）、libglew-dev、libgl1-mesa-dev、**libssl-dev**。
- **OpenCV 4.10.0 + CUDA 已从源码编译**（安装在 /usr/local，含 cudawarping/cudaimgproc/cudastereo/cudev 等模块）。
- ORB-SLAM3 已成功编译多次（libORB_SLAM3.so 产出过）。
- torch 2.6.0 的 C++ API 兼容补丁已打（见 §3）。

---

## 3. M2 Photo-SLAM 踩过的坑（按时间顺序，全部已定位）

| # | 问题 | 根因 | 解决 |
|---|---|---|---|
| 1 | 仓库结构 ≠ 文档 | fork 无 `gaussian_splatting/`，是 `cuda_rasterizer/`+`viewer/`；主程序是纯 C++ | 按实际结构走 |
| 2 | Photo-SLAM C++ 需 LibTorch | CMake `find_package(Torch REQUIRED)` | 用 conda torch 2.6.0 的 LibTorch（Torch_DIR 指向 site-packages/torch/share/cmake/Torch）|
| 3 | `CUDA_ARCHITECTURES "75;86"` 硬编码 | 没 4090 的 sm_89 | CMakeLists 第80行改为 `"89"` |
| 4 | ORB-SLAM3 缺 `openssl/md5.h` | README 依赖列表漏 libssl-dev | apt 装 libssl-dev |
| 5 | 两个编译进程并发污染 | 原 build.sh 无 `set -e`，报错后继续；我又起了第二个 | 加 `set -e` + `mkdir -p`，杀掉全部重来 |
| 6 | Photo-SLAM 需 CUDA OpenCV | 需要 `cudawarping/cudaimgproc/cudastereo` | 源码编 OpenCV 4.10.0+CUDA |
| 7 | OpenCV 报需 `cudev` 模块 | cudev 在 opencv_contrib | 加 opencv_contrib + BUILD_LIST 含 cudev |
| 8 | OpenCV cmake 卡在下载 IPPICV | raw.githubusercontent.com 被墙 | `-DWITH_IPP=OFF` |
| 9 | torch 2.6 删了 `c10::guts::to_string` | C++ API 变更（README 说 ≤2.1.2）| 改为 `param.unsafeGetTensorImpl()`（见下方补丁）|
| 10 | `torch::cat({...})` 构造失败 | torch 2.6 的 ArrayRef 无 initializer_list 构造 | 改为 `std::vector<torch::Tensor>({...})` |
| 11 | **C++ ABI 不匹配（核心难点）** | conda torch 是**旧 ABI**（`-D_GLIBCXX_USE_CXX11_ABI=0`），系统 OpenCV/jsoncpp 是新 ABI → `std::string` 符号链接不上 | 见下方 ABI 处理 |
| 12 | 旧 ABI jsoncpp 装 /usr/local 后 cmake 崩 | cmake 自己也链 jsoncpp | 移除，改装 /opt/oldabi |
| 13 | ORB-SLAM3 是新 ABI、Photo-SLAM 是旧 ABI | 只有 Photo-SLAM 的 CMake 用了 TORCH_CXX_FLAGS | `add_definitions` 给所有子项目加旧 ABI |
| 14 | `make -j8` 竞态 `.o.d` 文件报错 | 并行编译竞态 | 降到 `make -j4` |
| 15 | **boost 序列化 ABI 不匹配**（最新）| 旧 ABI 的 libORB_SLAM3.so 引用 `boost::archive::...load(std::string&)`（旧 ABI），而系统 libboost_serialization 是新 ABI | **旧 ABI 路线到此宣告走不通**（boost 也要旧 ABI，且后面可能还有更多库）|

### 关键补丁（已在 Route B 上改好，勿重复改）
1. **torch 2.6 API 补丁**（`src/gaussian_model.cpp`、`src/gaussian_mapper.cpp`）：
   - `c10::guts::to_string(param.unsafeGetTensorImpl())` → `param.unsafeGetTensorImpl()`（torch 2.6 的 optimizer state map 用 `void*` 指针作 key，不再用 string）
   - `torch::cat({...}, dim)` → `torch::cat(std::vector<torch::Tensor>({...}), dim)`
2. **CUDA arch**：`CMakeLists.txt` 第80行 `CUDA_ARCHITECTURES "89"`。
3. **build.sh**：顶部加了 `set -e`；`mkdir build`→`mkdir -p build`；每个 cmake 加了
   `-DOpenCV_DIR=/usr/local/lib/cmake/opencv4`；Photo-SLAM 段加了 `-DTorch_DIR=... -Djsoncpp_DIR=/opt/oldabi/lib/cmake/jsoncpp -DCMAKE_BUILD_RPATH=/opt/oldabi/lib`；make 全改 `-j4`。
4. **ABI**：给 `ORB-SLAM3/CMakeLists.txt`、`ORB-SLAM3/Thirdparty/{DBoW2,g2o,Sophus}/CMakeLists.txt`
   顶部加了 `add_definitions(-D_GLIBCXX_USE_CXX11_ABI=0)`（Photo-SLAM 自己已通过 TORCH_CXX_FLAGS 得到该宏）。

### ABI 处理现状（重要，请先理解）
- 目标：**全部统一为旧 ABI**（`_GLIBCXX_USE_CXX11_ABI=0`），因为 conda torch 是旧 ABI。
- 已做：OpenCV（/usr/local）旧 ABI、jsoncpp（/opt/oldabi）旧 ABI、ORB-SLAM3/DBoW2/g2o/Sophus 加 add_definitions、Photo-SLAM 由 TORCH_CXX_FLAGS 提供。
- 注意：系统 /usr 下的 jsoncpp(1.9.5) 和 OpenCV(4.5.4) 仍是新 ABI，用于 cmake 等系统工具，**别动它们**。

---

## 4. 当前状态（构建已停止）

- **构建已主动停止**（旧 ABI 路线走到 boost 库时宣告走不通，继续无意义）。
- 最后错误：`libORB_SLAM3.so: undefined reference to boost::archive::...load(std::string&)` ——
  ORB-SLAM3 已是旧 ABI，但系统 libboost_serialization 是新 ABI。
- 已确认旧 ABI 路线的问题链：torch(旧) → OpenCV(已改旧) → jsoncpp(已改旧) → **boost(未改，卡在这)** →
  可能还有 eigen 之外的更多 C++ 依赖。**不建议继续这条路线。**

### 强烈建议的下一步（请另一个 session 优先走这条路）
**放弃旧 ABI 路线，改用 cxx11-abi LibTorch 2.1.2（新 ABI），让全部依赖保持系统默认新 ABI。**
这是 Photo-SLAM README 官方推荐的做法，也正好绕开 torch 2.6 的 API 补丁。具体见 §6。

### Route B 上的关键脚本（排查时可看）
- `/root/build_photoslam.sh` —— 编译 wrapper（含 `set -e`、CUDA_HOME、调 build.sh）
- `/root/build.sh`（在 ~/Photo-SLAM 内）—— 已打补丁的构建脚本
- `/root/build_opencv_cuda.sh`、`/root/rebuild_abi.sh` —— OpenCV/jsoncpp 源码编译脚本
- 日志：`/root/build_photoslam.log`、`/root/opencv_build.log`、`/root/abi_rebuild.log`

---

## 5. 编译成功后 M2 还差什么

1. 确认 `~/Photo-SLAM/bin/` 下二进制（如 `photo_slam` 或类似名）产出。
2. Python 评估依赖（可选）：`pip install evo numpy scipy scikit-image lpips pillow tqdm plyfile`。
3. 准备一段**手机录的视频**（720p@15fps 室内慢速扫动）+ 相机内参（fx/fy/cx/cy/畸变，从手机 CameraCharacteristics 读）。
4. 填 cfg（ORB_SLAM3 的 monocular 配置 + gaussian_mapper 配置），跑：
   `./bin/<variant> ./ORB-SLAM3/Vocabulary/ORBvoc.txt <SLAM.yaml> <gaussian.yaml> <视频或图片目录> <输出目录> no_viewer`
5. 验收：能重建出可辨认场景、能导出高斯 + 轨迹。

---

## 6. 推荐路线（cxx11-abi LibTorch，新 ABI，README 官方做法）

旧 ABI 路线已失败（§3 第15号坑）。请改用这条：

1. **下载** `libtorch-cxx11-abi-shared-with-deps-2.1.2+cu121.zip`（~2.3GB）。
   建议本机 proxy 下载 + scp 上传（Route B 直连慢）。本机沙箱可 `export http_proxy=http://127.0.0.1:7890`。
2. 解压到 `~/libtorch`，改 build.sh 里 Photo-SLAM 段的 `Torch_DIR` 指向 `~/libtorch/share/cmake/Torch`。
3. **撤销旧 ABI 的改动**（回系统默认新 ABI）：
   - 删掉 4 个子项目 CMakeLists 顶部的 `add_definitions(-D_GLIBCXX_USE_CXX11_ABI=0)`；
   - OpenCV 需重新用**新 ABI（默认）**编译（去掉 rebuild_abi.sh 里的 ABI 标志，重跑 build_opencv_cuda.sh 即可）；
   - jsoncpp 回系统版（`/usr` 的 1.9.5），删掉 build.sh 里 `jsoncpp_DIR=/opt/oldabi` 和 `CMAKE_BUILD_RPATH=/opt/oldabi/lib`。
4. **撤销 torch 2.6 API 补丁**（2.1.2 不需要）：`git -C ~/Photo-SLAM checkout -- src/gaussian_model.cpp src/gaussian_mapper.cpp`。
5. 重编 Photo-SLAM（build.sh，此时 Torch 是新 ABI 的 2.1.2，OpenCV/jsoncpp/boost 都是系统新 ABI，天然一致）。

> 说明：这条路线里 OpenCV 仍需保留 CUDA 模块（cudewarping/cudaimgproc/cudastereo/cudev），
> 所以 build_opencv_cuda.sh（含 contrib、-DWITH_IPP=OFF）依然要跑，只是**去掉 ABI 标志**。

---

## 7. ✅ cxx11-abi 路线执行结果（本 session 已完成，编译成功）

**结论：Photo-SLAM 编译成功。** 上述 §6 路线已完整执行并验证通过，`BUILD_DONE` exit=0。

### 实际执行的关键差异/补充
1. LibTorch 上传：scp 受本地 Mac 上行限制（~0.6MB/s）太慢，改为 **Route B 直连 r2 CDN 断点续传**（`curl -L -C -`），稳定 ~1–2.7MB/s。
2. examples 里 `c10Alloc::Stat`/`StatType` 的 torch 2.6 补丁（上一 session 遗留）也一并 `git checkout` 撤销 —— 2.1.2 原生支持，无需补丁。
3. 最终 build.sh：5 段 cmake 均无 `CMAKE_CXX_FLAGS` ABI 标志、无 `jsoncpp_DIR`/`RPATH`；Photo-SLAM 段 `Torch_DIR=/root/libtorch/share/cmake/Torch`；`make -j4`。

### 验证结果（全部通过）
- `bin/` 产出 7 个可执行文件：`tum_mono` / `tum_rgbd` / `euroc_stereo` / `replica_mono` / `replica_rgbd` / `train_colmap` / `view_result`。
- `lib/` 产出 5 个核心库：`libcuda_rasterizer` / `libgaussian_mapper` / `libgaussian_viewer` / `libimgui` / `libsimple_knn`。
- `libORB_SLAM3.so` 含 108 个 cxx11 符号（新 ABI，与全链路一致）。
- `ldd` 确认：OpenCV 4.10 → `/usr/local`（新 ABI）、LibTorch 2.1.2 → `/root/libtorch`、jsoncpp/boost → 系统 `/usr`（新 ABI），无 `not found`。

### 下一步（M2 冒烟）
1. 准备一段手机视频 + 相机内参（fx/fy/cx/cy/畸变），填 ORB-SLAM3 monocular cfg + gaussian_mapper cfg。
2. 跑：`./bin/tum_mono ./ORB-SLAM3/Vocabulary/ORBvoc.txt <slam.yaml> <gaussian.yaml> <输入> <输出> no_viewer`（RGB-D 场景用 `tum_rgbd`）。
