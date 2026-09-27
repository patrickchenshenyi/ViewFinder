# Photo-SLAM 云镜像重建清单（RTX 4090 已验证编译通过）

> 本文是「重建矩池云镜像」的最终落档，结论来自 Route B 实例（Ubuntu 22.04 + CUDA 12.1 + RTX 4090）**实测编译通过** Photo-SLAM 的完整过程。
> 配套：`docs/plan.md`（总体计划）、`docs/gs_slam_deployment.md`（选型）、`docs/m2_photoslam_handoff.md`（M2 完整踩坑记录）、`docs/monoGS_4090_image_checklist.md`（M1 镜像清单）。

---

## 0. 核心结论（先看这个，避免踩最大坑）

Photo-SLAM 主程序是**纯 C++**（ORB-SLAM3 + LibTorch + CUDA 光栅化器），构建最大的坑是 **C++ ABI 不匹配**：

- conda 自带的 torch 是**旧 ABI**（`_GLIBCXX_USE_CXX11_ABI=0`），而 Ubuntu 22.04 系统库 OpenCV / jsoncpp / **boost** 全是**新 ABI**。
- 若用 conda torch 的 LibTorch，会陷入「把每个 C++ 依赖逐个重编成旧 ABI」的无底洞（OpenCV → jsoncpp → boost → …），实测卡在 boost，此路不通。
- ✅ **正确做法**：用独立的 **cxx11-abi 版 LibTorch 2.1.2（新 ABI）**，全链路保持系统默认新 ABI，零 ABI 改造。
- 同时 2.1.2 是 Photo-SLAM README 声明的支持版本，**不需要 torch 2.6 的任何 API 补丁**。

> 因此：Photo-SLAM 的 C++ 构建**不依赖 conda torch**（只用独立 LibTorch）；conda 仅用于 M1 MonoGS。

---

## 1. 基础镜像要求（硬性）

| 项 | 要求 | 说明 |
|---|---|---|
| GPU | RTX 4090（Ada, sm_89 / compute cap 8.9） | 需要 CUDA ≥ 11.8 |
| 系统 | Ubuntu 22.04 | 20.04 亦可 |
| 驱动 | ≥ 525（实测 595.91.07 正常） | 驱动级 CUDA 13.2，向后兼容 |
| CUDA 工具链 | **nvcc 12.1**（`/usr/local/cuda-12.1`） | 编译 OpenCV+CUDA 和光栅化器必需 |
| 编译工具 | cmake、gcc/g++（11.x） | — |
| conda | 可选（仅 MonoGS 需要） | Photo-SLAM 不依赖 |

---

## 2. 一键重建步骤（按顺序）

### 2.1 系统依赖（apt）

```bash
apt-get update && apt-get install -y \
  cmake libeigen3-dev libboost-all-dev libjsoncpp-dev libopengl-dev \
  mesa-utils libglfw3-dev libglm-dev libopencv-dev libglew-dev \
  libgl1-mesa-dev libssl-dev
```

> 注意 `libssl-dev`（README 漏了它，ORB-SLAM3 需要 `openssl/md5.h`）。

### 2.2 OpenCV 4.10.0 + CUDA（**新 ABI 默认，不要加任何 ABI 标志**）

```bash
git clone --depth 1 --branch 4.10.0 https://github.com/opencv/opencv.git ~/opencv
git clone --depth 1 --branch 4.10.0 https://github.com/opencv/opencv_contrib.git ~/opencv_contrib

cd ~/opencv && mkdir -p build && cd build
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
cmake -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/usr/local \
  -DWITH_CUDA=ON \
  -DCUDA_TOOLKIT_ROOT_DIR=/usr/local/cuda-12.1 \
  -DCUDA_ARCH_BIN=8.9 \
  -DWITH_CUDNN=OFF -DOPENCV_DNN_CUDA=OFF -DWITH_IPP=OFF \
  -DOPENCV_EXTRA_MODULES_PATH=/root/opencv_contrib/modules \
  -DBUILD_LIST=core,imgproc,imgcodecs,calib3d,features2d,flann,video,videoio,highgui,cudev,cudawarping,cudaimgproc,cudastereo,cudaarithm,cudafilters \
  -DBUILD_opencv_python3=OFF -DBUILD_opencv_python_bindings_generator=OFF \
  -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_opencv_apps=OFF \
  -DBUILD_JAVA=OFF -DBUILD_opencv_js=OFF \
  ..
make -j8 && make install && ldconfig
```

> ⚠️ 关键：**不要**加 `-DCMAKE_CXX_FLAGS="-D_GLIBCXX_USE_CXX11_ABI=0"`（那是旧 ABI 的坑）。`-DWITH_IPP=OFF` 必须加（IPPICV 从 raw.githubusercontent 下载被墙）。

### 2.3 LibTorch 2.1.2（cxx11-abi，新 ABI）

```bash
# 下载 2.33GB；国内直连慢，建议本地 proxy 下载后上传，或 Route B 直连 r2 CDN 断点续传
cd ~
curl -L -C - -o libtorch.zip \
  'https://download.pytorch.org/libtorch/cu121/libtorch-cxx11-abi-shared-with-deps-2.1.2%2Bcu121.zip'
unzip -q -o libtorch.zip   # 解压出 ~/libtorch，含 share/cmake/Torch/TorchConfig.cmake
```

> 必须选 **cxx11-abi** 变体（默认版 `libtorch-shared-with-deps` 是旧 ABI，会重蹈覆辙）。

### 2.4 克隆仓库（无子模块，vendored）

```bash
git clone https://github.com/HuajianUP/Photo-SLAM.git ~/Photo-SLAM
```

> 该 fork 的 `.gitmodules` 为空，ORB-SLAM3 + Thirdparty（DBoW2/g2o/Sophus）已直接包含，**无需 `submodule update`**。注意结构是 `cuda_rasterizer/` + `viewer/`（不是文档里的 `gaussian_splatting/`）。

### 2.5 补丁：CUDA 架构加 sm_89

`CMakeLists.txt` 第 80 行，把硬编码的 `"75;86"` 改成 `"89"`（否则 4090 跑不了）：

```cmake
set_target_properties(cuda_rasterizer PROPERTIES CUDA_ARCHITECTURES "89")
```

### 2.6 build.sh（最终正确版，无 ABI 标志）

把 `~/Photo-SLAM/build.sh` 替换为下面内容（关键：每段 cmake 只加 `OpenCV_DIR`，**不加** `CMAKE_CXX_FLAGS`；Photo-SLAM 段 `Torch_DIR` 指向 LibTorch）：

```bash
set -e
# DBoW2
cd ./ORB-SLAM3/Thirdparty/DBoW2
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4
cd ../../g2o

# g2o
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4
cd ../../Sophus

# Sophus
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4

# ORB_SLAM3
cd ../../../Vocabulary
echo "Uncompress vocabulary ..."
tar -xf ORBvoc.txt.tar.gz
cd ..
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4

# Photo-SLAM
echo "Building Photo-SLAM ..."
cd ../..
mkdir -p build && cd build
cmake .. -DTorch_DIR=/root/libtorch/share/cmake/Torch -DOpenCV_DIR=/usr/local/lib/cmake/opencv4
make -j4
```

### 2.7 编译

```bash
cd ~/Photo-SLAM
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
bash build.sh        # 结束应看到 BUILD_DONE；用 -j4 避免并行竞态
```

---

## 3. 验证结果（Route B 实测）

| 项 | 结果 |
|---|---|
| `bin/` 可执行文件 | ✅ 7 个：tum_mono / tum_rgbd / euroc_stereo / replica_mono / replica_rgbd / train_colmap / view_result |
| `lib/` 核心库 | ✅ 5 个：libcuda_rasterizer / libgaussian_mapper / libgaussian_viewer / libimgui / libsimple_knn |
| ABI 一致性 | ✅ libORB_SLAM3.so 含 108 个 cxx11 符号（新 ABI） |
| 运行时链接 | ✅ OpenCV 4.10→/usr/local、LibTorch 2.1.2→/root/libtorch、jsoncpp/boost→系统 /usr，无 `not found` |

---

## 4. 运行命令

```bash
cd ~/Photo-SLAM
./bin/tum_mono \
    ./ORB-SLAM3/Vocabulary/ORBvoc.txt \
    ./cfg/ORB_SLAM3/Monocular/TUM/<你的slam.yaml> \
    ./cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml \
    <图片序列目录> \
    <结果输出目录> \
    no_viewer
```

参数顺序：词典 → ORB-SLAM3 相机配置 → gaussian_mapper 配置 → 序列目录 → 输出目录 → 可选 `no_viewer`（headless 必加）。

---

## 5. 输入数据格式（手机视频 → TUM 序列）

`tum_mono` / `tum_rgbd` 读的是 **TUM 图片序列**，不是视频文件。目录结构：

```
<序列目录>/
├── rgb.txt          # 前 3 行是注释（会被跳过），之后每行「时间戳 相对文件名」
└── rgb/             # RGB 帧（或直接放根目录，文件名与 rgb.txt 一致）
    000001.png
    000002.png
    ...
```

`rgb.txt` 示例（时间戳单位秒，单调递增）：

```
# color images
# file: 'rgbd_dataset_freiburg1_desk.bag'
# timestamp filename
1305031452.791720 rgb/1305031452.791720.png
1305031452.825087 rgb/1305031452.825087.png
...
```

手机视频转 TUM 序列（帧率与 `Camera.fps` 保持一致，如 30fps）：

```bash
mkdir -p seq/rgb
ffmpeg -i input.mp4 -r 30 -q:v 2 seq/rgb/%06d.png
# 生成 rgb.txt（时间戳 = 帧序号 / fps）
python3 - <<'PY'
import os
fps = 30.0
files = sorted(f for f in os.listdir('seq/rgb') if f.endswith('.png'))
with open('seq/rgb.txt', 'w') as w:
    w.write("# color images\n# file\n# timestamp filename\n")
    for i, f in enumerate(files):
        w.write(f"{i/fps:.6f} rgb/{f}\n")
PY
```

**手机录像建议**：30fps、720p–1080p、**慢速平滑扫动**、光照充足、场景有纹理（白墙/纯色会跟踪失败）、避免快速旋转和强运动模糊。

---

## 6. cfg 填写指导

### 6.1 ORB-SLAM3 相机配置（`cfg/ORB_SLAM3/Monocular/TUM/*.yaml`）

复制 `tum_freiburg1_desk.yaml` 改这几项即可，其余（ORBextractor / Viewer）保持默认：

```yaml
Camera.type: "PinHole"

# 相机内参（从手机 CameraCharacteristics 读，单位像素）
Camera1.fx: 517.306408
Camera1.fy: 516.469215
Camera1.cx: 318.643040
Camera1.cy: 255.313989

# 畸变（OpenCV 径向+切向模型，未知可先全填 0）
Camera1.k1: 0.0
Camera1.k2: 0.0
Camera1.p1: 0.0
Camera1.p2: 0.0
Camera1.k3: 0.0

Camera.fps: 30            # 与抽帧帧率一致
Camera.RGB: 1             # 程序已 BGR→RGB，填 1
Camera.width: 640         # 与抽帧分辨率一致
Camera.height: 480
```

> 内参换算：Android `CameraCharacteristics` 里 `LENS_FOCAL_LENGTH`(f_mm) 与 `SENSOR_INFO_PHYSICAL_SIZE`(w_mm) → `fx = f_mm / w_mm * width`；或直接用出厂标定。没有内参时，可用 `fx=fy=width, cx=width/2, cy=height/2` 粗略起步。

### 6.2 gaussian_mapper 配置（`cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml`）

**一般无需改动**，直接用 `tum_mono.yaml` 即可。这是高斯建图/优化参数，冒烟测试用默认值。真正想调时才关注：

| 参数 | 默认 | 作用 |
|---|---|---|
| `Model.sh_degree` | 3 | 球谐阶数（越高越精细、越吃显存） |
| `Camera.z_near` / `z_far` | 0.01 / 100.0 | 单目深度范围（米） |
| `Optimization.max_num_iterations` | 30100 | 建图总迭代 |
| `Mapper.large_translation_threshold` | 1.0 | 触发新关键帧的位移阈值 |

RGB-D 场景则用 `cfg/gaussian_mapper/RGB-D/TUM/tum_rgbd.yaml` + `cfg/ORB_SLAM3/RGB-D/TUM/*.yaml`。
