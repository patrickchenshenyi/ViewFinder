# 服务端 GS-SLAM 选型与部署

> 面向「Android 手机 RGB 推流 → 云 GPU 在线重建 → Web 展示」的 toy demo。
> 目标环境：Ubuntu 22.04 + NVIDIA GPU（12–16 GB 显存）。
> 注意：各仓库迭代很快，**具体命令行 flag 以对应仓库 README 为准**，本文给出的是稳定主流程。

---

## 1. 选型结论

我们的输入是**单目 RGB 流**，且**未来也不加深度传感器**（永久单目）。因此 RGB-D 类方法（RTG-SLAM/SplaTAM/LoopSplat）**排除**，只考虑单目方法；尺度漂移作为已知代价接受。

| 框架 | 输入 | 速度 | 鲁棒性(回环/重定位) | 安装难度 | 结论 |
|---|---|---|---|---|---|
| **MonoGS** ([CVPR'24]) | 单目 RGB | ~3fps(3090) | 无回环，漂移 | ★☆☆ Python+CUDA | **先跑通它**：最简单、经典基线 |
| **Photo-SLAM** ([CVPR'24]) | 单目/RGB-D/双目 | 实时(可 Jetson) | ✅ ORB-SLAM3 回环+重定位 | ★★★ C++(Pangolin/ORB-SLAM3)+Python | **接实时流用它**：鲁棒、直接读相机/RTSP |
| RTG-SLAM ([SIGGRAPH'24]) | RGB-D | 60fps+ 追踪 | 无回环 | ★★ C++/CUDA | 仅当有真实深度 |
| SplaTAM ([CVPR'24]) | RGB-D | 实时 | 无回环 | ★★ Python+CUDA | 仅当有真实深度 |
| LoopSplat ([ECCV'24]) | RGB-D | 实时 | ✅ 回环 | ★★ | 仅当有真实深度 |

**推荐路径（两阶段）**

1. **里程碑 1（验证环境 + 打通链路）**：用 **MonoGS** 在 TUM/EuRoC 公开数据集上跑通，确认 CUDA/PyTorch/光栅化器可用。
2. **里程碑 2（接手机实时流）**：切到 **Photo-SLAM**，用 ORB-SLAM3 读 RTSP 实时重建（回环/重定位更稳，适合手持+室内外）。

> 为什么接实时流不直接用 MonoGS：MonoGS 主要面向离线数据集，接"持续增长的帧流"要自己改 Dataset；Photo-SLAM 的 ORB-SLAM3 天然支持读相机/视频/RTSP。

---

## 2. 云 GPU 实例准备

- **规格**：RTX 4090 24GB（你已选）。
- **系统镜像（矩池云）**：选 **Ubuntu 22.04 + CUDA 12.1（或 11.8）+ PyTorch 2.x + conda** 的官方镜像。
  - **为什么必须 CUDA ≥ 11.8**：RTX 4090 是 Ada Lovelace（SM_89），CUDA 11.8 才开始完整支持；**避开 11.3/11.6/11.7 和 PyTorch 1.x 的老镜像**。
  - 镜像里自带的 PyTorch 版本不重要（后面会 `conda create` 独立环境），关键是「驱动支持 4090 + CUDA 工具链 ≥ 11.8 + 有 conda」。
  - 若列表里同时有 CUDA 11.8 和 12.1，**优先 12.1**（更新）；个别仓库若 pin 旧 torch，再在独立环境里覆盖为 torch 2.x。
- **国内**：矩池云 / AutoDL；**国外**：runpod / Lambda。
- 装好后验证（重要）：

```bash
nvidia-smi                       # 应显示 4090，CUDA 驱动版本 ≥ 12.x
python -c "import torch; print(torch.cuda.is_available(), torch.__version__)"
```

```bash
nvidia-smi                       # 确认驱动/显存
python -c "import torch; print(torch.cuda.is_available(), torch.__version__)"
# 无 conda 则装 miniconda:
# wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh && bash Miniconda3-latest-Linux-x86_64.sh
```

---

## 3. 里程碑 1：MonoGS（离线数据集验证）

仓库（`--recursive` 必须带上，子模块含 CUDA 光栅化器）：
- 现名 MonoGS：<https://github.com/muskie82/MonoGS>
- ⚠️ 原地址 `musketyr/gaussian-splatting-slam` **已改名/失效**（返回 "Repository not found"），不要再用了

```bash
# 国内直连慢，推荐走加速镜像： git clone https://githubproxy.cc/https://github.com/muskie82/MonoGS.git --recursive
git clone https://github.com/muskie82/MonoGS.git --recursive
cd MonoGS

# 建环境（environment.yml 里 env 名为 MonoGS，含 torch/torchvision）
conda env create -f environment.yml
conda activate MonoGS

# 编译 CUDA 子模块（耗时最长的一步）
pip install -e submodules/diff-gaussian-rasterization
pip install -e submodules/simple-knn        # 仅评估用，可后装
```

跑通样例（TUM RGB-D 序列，只取 RGB；EuRoC/Replica 同理）：

```bash
python slam.py --config configs/mono/tum/fr3_office.yaml
```

- 若需要下载预训练权重（光流/深度先验），按仓库 README 的下载脚本执行。
- 输出：`outputs/...` 下会有渲染帧、日志，以及可导出的 splat 结果。

**验证标准**：CUDA 光栅化器能编译、`slam.py` 能跑完一段序列不 OOM。这一步过了，说明环境 OK。

---

## 4. 里程碑 2：Photo-SLAM（接实时手机流）

仓库（原仓库若失效，用 fork 镜像）：
- 官方：<https://github.com/HUANG-ZHIHAO/PHOTO-SLAM>
- fork 镜像：<https://github.com/HuajianUP/Photo-SLAM>

```bash
git clone https://github.com/HuajianUP/Photo-SLAM.git
cd Photo-SLAM

# 1) C++ 端：ORB-SLAM3 + Thirdparty(Pangolin/DBoW2/g2o)
conda create -n photoslam python=3.8 -y
conda activate photoslam
sudo apt-get install -y cmake libeigen3-dev libopencv-dev libglew-dev libgl1-mesa-dev
./build.sh

# 2) Python 端：Gaussian mapper（PyTorch + nerfstudio 系）
pip install -r gaussian_splatting/requirements.txt
pip install -e gaussian_splatting
```

### 4.1 相机标定参数（YAML）

手机主摄内参需要填进配置（可从手机 `CameraCharacteristics` 读到，或标定一次）：
`fx, fy, cx, cy, k1, k2, p1, p2, fps, 分辨率`。

### 4.2 实时流输入（两种方式）

**方式 A（推荐，最简单）**：Android 装 **IP Webcam** App，开 RTSP 服务，Photo-SLAM 直接读：

```bash
./bin/PHOTO-SLAM ./configs/EuRoC.yaml    # 把 YAML 里 Camera.type 改成 Monocular，Dataset 指向 rtsp 流/相机索引
```

（ORB-SLAM3 用 OpenCV `VideoCapture` 打开，RTSP URL 或 `/dev/video0` 均可，具体字段名见仓库 config 示例。）

**方式 B（ffmpeg 兜底落盘）**：任何推流方案都能用：

```bash
ffmpeg -rtsp_transport tcp -i "rtsp://<phone-ip>:8080/h264_ulaw.sdp" \
       -vf fps=10 -q:v 2 frames/%06d.jpg
```

然后用 TUM/单目数据格式喂给 ORB-SLAM3。

> Photo-SLAM 跑通后，把「重建好的高斯 + 每帧位姿」导出（或边跑边 dump），就是下一阶段喂给 Web 渲染器的数据源。

---

## 5. 手机 → 服务器推流（供里程碑 2 使用）

- **零代码**：Android 装 `IP Webcam`（或同类），局域网/内网穿透出 RTSP。
- **进阶**：自研 CameraX App，用 libwebrtc 推 WebRTC（低延迟），服务器端用 `aiortc`/GStreamer 收流转帧。
- **兜底**：任何方案都能用 §4.2 方式 B 的 ffmpeg 落盘，确保与 SLAM 解耦。

---

## 6. 常见坑

| 坑 | 处理 |
|---|---|
| CUDA/驱动/PyTorch 版本不匹配 | 先 `nvidia-smi` + `torch.cuda.is_available()` 对齐，再看仓库建议的 CUDA 版本 |
| 子模块没拉全 | clone 时务必 `--recursive`，否则 diff-gaussian-rasterization 是空的 |
| OOM（显存不够） | 降输入分辨率(480p/720p)、降 fps(10)、限制高斯数上限 |
| 单目尺度漂移（场景大小不准） | 接受即可；后续可加 IMU 或真实深度改善 |
| 快速运动丢跟踪 | 降速、用带回环的 Photo-SLAM/LoopSplat |
| 原仓库 404 | 用文中 fork/镜像，或 GitHub 搜项目名取活跃 fork |

---

## 7. 下一步

1. 云实例上按 §3 跑通 MonoGS（TUM fr3_office）。
2. 按 §4 装 Photo-SLAM，用录一段手机视频离线跑通。
3. 接 RTSP 实时流（§5），确认在线重建。
4. 从 Photo-SLAM 导出高斯 + 位姿 → 进入「增量 splat 传输协议 + Web 渲染」阶段。

[CVPR'24]: https://github.com/musketyr/gaussian-splatting-slam
[SIGGRAPH'24]: https://github.com/MisEty/RTG-SLAM
[ECCV'24]: https://github.com/LoopSplat/LoopSplat
