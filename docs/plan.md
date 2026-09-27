# ViewFinder — 总体方案与实施计划

> 目标：Android 手机实时视频 → 云端 3D Gaussian Splatting 在线重建 → PC 浏览器实时展示。
> 性质：个人兴趣项目，toy demo。

---

## 0. 当前进度总览（2026-09-24）

**核心结论：全链路已跑通 ✅** —— 手机 IP Webcam → 云端 Photo-SLAM 增量建图 → WebSocket 推送 → 浏览器实时渲染，端到端验证 PASS。

**最新重建质量：25.97 dB**（正确内参 fx=892.4 + quality 70 + 干净起点；对比旧粗值 fx=1280 + q50 时的 19.33 dB，**+6.64 dB**）。

| 里程碑 | 状态 | 关键产出 |
|---|---|---|
| M0 环境 | ✅ | 矩池云 4090 + CUDA 12.1 |
| M1 MonoGS 冒烟 | ✅ | 环境验证通过 |
| M2 Photo-SLAM 离线 | ✅ | cxx11-abi LibTorch 2.1.2 编译通过 |
| M3 实时流 | ✅ | 边拍边建全链路跑通 |
| M4 增量+传输 | ✅ | 真增量输入 + 双轨训练 + delta 导出 + WebSocket 推送 + 浏览器实时渲染 |
| M5 收尾 | 🔄 进行中 | 采集质量 / 稳定性 / 体验 |
| M6 取景/构图推荐 🆕 | 🔄 进行中（M6.0–M6.2 完成） | 独立计划见 `docs/plan_m6.md` |
| M7 增量下行推流 🆕 | ✅ Phase A 已完成（M7.1 C++ / M7.2 Node / M7.3 浏览器 全部部署并验证） | 独立计划见 `docs/plan_m7.md` |

**已完成的关键能力：**
- 真增量建图（`tum_mono` 轮询新帧 + `DONE` 哨兵结束）
- 双轨训练（在线集成 + 后台精修到 10000 迭代）
- 稳定高斯 ID + delta 导出（added / removed）
- WebSocket 推送 + 浏览器实时渲染（~5s 整幅刷新）
- 浏览器「开始/停止推流」按钮（采集起点由用户掌控，解决「静止帧拖沓 + 起点不清」）
- 真机内参（一加 15 出厂标定 fx=892.4，16:9 居中裁剪实测验证，已上云）

**已知问题与对策：**
- 网络带宽不足（上下行慢）：根因是全链路挤手机热点一条上行；**已计划更换网络设备 / 加独立 AP 直接提高带宽**（从而进一步提高视频码率）。
- 畸变 k1..k5 未标定（假定 0）：待棋盘格标定。
- 单目尺度漂移：接受（toy）。
- 操作指南见 `docs/operations_guide.md`。

---

## 1. 已锁定的永久决策

| 决策 | 取值 | 影响 |
|---|---|---|
| 深度 | **永久单目 RGB，不加深度传感器** | 重建只用 RGB；尺度有歧义、会漂移（可接受）；RGB-D 类方法（RTG-SLAM/SplaTAM/LoopSplat）**排除** |
| 计算 | 云端 **RTX 4090 24GB**（矩池云） | 算力富余，延迟目标可从 3s 压到 ~1–2s |
| 延迟 | **端到端 ≤ 3s**（高质量离线可放宽，理想 <10s） | 采用「批处理」而非逐帧实时；训练迭代数与延迟互斥，见 M3.7 |
| 场景 | 室内 + 室外 | 需要带回环/重定位的方法 |
| 展示端 | 仅 PC 浏览器（WebGPU/WebGL） | 单观看者，带宽无压力 |

---

## 2. 端到端架构

```
Android 手机 (CameraX / IP Webcam)
        │  RGB 流 (720p @ 10–15fps, H.264, RTSP/WebRTC)
        ▼
云 RTX 4090 ── Photo-SLAM (单目) 增量建图  [可选: Depth Anything v2-S 尺度先验]
        │  增量 splat delta (量化压缩) + 全量快照
        ▼  WebSocket
PC 浏览器 ── gsplat.js (WebGPU) 动态增删改 splat buffer
```

---

## 3. 关键选型

| 环节 | 选型 | 状态 |
|---|---|---|
| 重建（主） | **Photo-SLAM**（单目模式，ORB-SLAM3 回环+重定位） | 主路径 |
| 重建（兜底/验证） | **MonoGS**（单目，纯 Python，装环境最快） | 环境冒烟用 |
| 重建（进阶备选） | DROID-Splat / HI-SLAM2（单目，更新） | 仅当 Photo-SLAM 不稳再试 |
| 推流 | IP Webcam(RTSP) 起步 → 自研 CameraX + WebRTC | 由简到繁 |
| 传输 | WebSocket 二进制增量 delta + 量化 + 手动全量快照 | 自研 |
| Web | GaussianSplats3D（three.js/WebGL2，本地查看器已就绪 `tools/`） | 后续改造为动态 buffer |

---

## 4. 计算资源与参数决策（RTX 4090 24GB）

### 4.1 视频分辨率 / 帧率

| 参数 | 建议值 | 理由 |
|---|---|---|
| 分辨率 | **1280×720 (720p)** | 特征点/渲染不需要 1080p；720p 是质量与算力/带宽的最优点 |
| 帧率 | **10–15 fps** | 手持 SLAM 足够（≥10fps 防跟踪丢失）；省 GPU 与上行带宽 |
| 编码 | H.264 硬件编码 | 手机标配，~2 Mbps 上行 |

### 4.2 模型 / 重建参数

| 参数 | 建议值 | 理由 |
|---|---|---|
| 方法 | Photo-SLAM 单目模式 | 回环+重定位，室内外稳 |
| 高斯数上限 | **1–2M** | 720p 场景足够；4090/24GB 轻松容纳 |
| SH 阶数 | **0（RGB）起步，需要视点依赖再升 1–3** | 阶数翻倍→颜色参数量翻倍，toy 从 0 阶省内存 |
| 尺度先验（可选，后期） | Depth Anything v2-S | 缓解单目尺度漂移，非首期必需 |
| 重建节奏 | 批处理，每 2–3s 一次增量更新 | 3s 预算内，避免逐帧实时压力 |

### 4.3 延迟预算（4090，实测余量充足）

| 阶段 | 耗时 |
|---|---|
| 手机采集+编码+上行 | 50–150 ms |
| 服务端跟踪 + 增量建图（批） | 0.5–1.5 s（主导，但远低于 3s） |
| splat 序列化 + 下行 | 50–200 ms |
| Web 渲染 | < 50 ms |
| **合计** | **≈ 1–2 s ✅（<3s 预算）** |

### 4.4 带宽（按上述参数）

| 方向 | 流量 |
|---|---|
| 上行（RGB） | 720p@15fps H.264 ≈ **1.5–2.5 Mbps** |
| 下行（增量 delta） | ≈ **3–10 Mbps** |
| 下行（全量快照，1–2M 高斯） | 突发 15–50 MB，仅手动同步时发生 |

---

## 5. 分阶段计划（Step by Step）

### M0 — 环境就绪 ✅ 已完成
- [ ] 镜像加载完成 → `nvidia-smi` 确认 4090 / 驱动 CUDA ≥ 12.x
- [ ] `python -c "import torch; print(torch.cuda.is_available())"`
- **验收**：4090 可见、CUDA 可用、conda 就绪

### M1 — 环境冒烟（MonoGS）✅ 已完成
- [ ] `bash scripts/setup_monogs.sh`
- [ ] 下载 TUM `fr3_office`，跑 `python slam.py --config configs/mono/tum/fr3_office.yaml`
- **验收**：CUDA 光栅化器编译通过、跑完一段序列不 OOM
- **产出**：确认环境 OK（这一步是纯验证，非最终方案）

### M2 — Photo-SLAM 离线跑通（手机录像）✅ 已完成
- [ ] 手机录一段 720p@15fps 室内+室外视频（先静止起步、缓慢扫动）
- [ ] 装 Photo-SLAM：`./build.sh` + 装 Gaussian mapper；**torch 覆盖为 2.x（cu118/cu121）**（官方 pin 1.13+cu116 在 4090 跑不了）
- [x] 填相机内参 YAML（从 `CameraCharacteristics` 读 fx/fy/cx/cy/畸变）→ 具体做法见 `docs/android_camera_intrinsics.md`，换算脚本 `scripts/android_intrinsics.py`
  > 2026-09-24 完成（一加 15 / PLK110 / Android 16，`dumpsys media.camera` 直读，**不用装 App**）：
  > `phone_1280x720.yaml` → fx=892.400 fy=892.253 cx=632.984 cy=357.109（出厂标定 2855.68/2855.21/2025.55/1526.75 → 16:9 居中裁剪 (0,384,4096,2304) → ×0.3125）。
  > **已用活体 session 实测验证**：IP Webcam 推流时 `cropRegion=[0 0 4096 3072]` 但
  > `rawCropRegion=[0 384 4096 2304]`（硬件实读 = 16:9 居中），zoom=1.0 / EIS=OFF，
  > HTTP 与 RTSP 流都是 1280x720 → 与 YAML 逐项差 0.000%（`scripts/verify_live_intrinsics.py --rtsp` → PASS）。
  > 详见该 YAML 头部注释；dump 存于 `tools/_captures/camera_dump.txt` 与 `camera_dump_live.txt`。
  > ⚠️ 仍未验证：画面剩余畸变是否为 0（需棋盘格标定，§7，暂缓）。
  > ⚠️ `phone_720p.yaml`（竖屏）与 `phone_640x360.yaml` 仍是粗值 fx=fy=W。
  > 竖屏 720x1280 应为 fx=892.253 fy=892.400 cx=362.891 cy=632.984（`--center-crop 16:9 --out 720,1280 --rotate 90`），待确认后再写入。
- [ ] 用单目模式跑通该视频，导出高斯 + 轨迹
- **验收**：能重建出可辨认场景、能导出 splat
- **产出**：第一份「手机→splat」离线产物

  > **实际结果**：已在 Route B 跑通——竖屏 720×1280 视频（微信压成 2/3，无裁切）→ 401 帧 → 30 关键帧 → 点云 `results/phone_720p_smoke/`，全程 ~40s、退出干净。构建走 **cxx11-abi LibTorch 2.1.2（新 ABI）** 路线（详见 `docs/photoslam_4090_image_checklist.md`）。

### M3 — 实时流打通（链路 + 运动素材跑通 ✅，进行质量调优）

> 目标：手机 IP Webcam → 云实例 ffmpeg 拉流 → 持续抽帧成 TUM → Photo-SLAM 增量建图。
> 网络决策（已定）：**主用 Mac SSH 反隧道**（零第三方依赖、复用已通的 SSH）；长期方案用自建 **frp**（已放弃 Tailscale）。
> 状态（2026-09-24）：运动素材跑通——横屏 720p 扫动 500 帧 → 81 关键帧 → 45,664 高斯、**回环检测成功**（Merge finished），全程 ~60–80s。质量仍偏低（训练只到 3881/30100 迭代），进入 3.7 质量调优。

**3.1 手机端（已完成 ✅）**
- [x] 装 IP Webcam 并验证可用。
- [x] 设置：1280×720、10–15fps、**关闭音频流**；Start server 记下 RTSP URL `rtsp://<手机LAN-IP>:8080/h264_ulaw.sdp`。

**3.2 网络打通 —— Mac SSH 反隧道（主用，已完成 ✅）**
- [x] Mac 能访问手机：`http://<手机LAN-IP>:8080` → HTTP 200。
- [x] 建立反隧道（本 session 后台任务持有）：`ssh -N -R 18080:<手机LAN-IP>:8080 -p <实例端口> root@<实例域名>`。
- [x] 云实例验证：`curl http://127.0.0.1:18080/` → 200；`ffprobe` RTSP → 1280×720@30。

**3.3 云实例拉流落盘（已完成 ✅）**
- [x] 装 ffmpeg CLI（云上原只有 libav* 库，无命令行工具）。
- [x] `scripts/m3_pull_rtsp.sh` 上云，`FPS=10 OUT_DIR=~/live` 运行，落盘 1748 帧 + `rgb.txt`（10fps 时间戳正确）。

**3.4 相机配置（已完成 ✅）**
- [x] `photo_slam_config/phone_1280x720.yaml`（横屏 1280×720、fx=fy=1280、cx=640、cy=360、fps=10），Photo-SLAM 正确读出 `Pinhole [1280 1280 640 360] 1280×720 10fps`。

**3.5 喂给 Photo-SLAM**
- [x] 快照批处理（冒烟）：跑通——加载 400 帧、地图初始化 252 点，但素材静止、无新关键帧、未出 splat。
- [ ] 真增量（正解）：轮询 `~/live/rgb/` 新帧 + 双轨训练 + splat 增量下发 → **并入 M4**。

**3.6 运动测试（已完成 ✅，出 splat）**
- [x] 横屏 720p 扫动 40–60s → 拉流 1246 帧 → 裁剪 150–649（500 帧）→ Photo-SLAM。
- [x] 结果：81 关键帧、45,664 高斯、**回环检测成功**（Merge finished），结果在 `results/m3_motion/`，全程 ~60–80s。
- [x] 关键发现：① 慢帧率根因 = 第二个 WiFi 上行 ~3 Mbps，换回原网段后 ~7.3 Mbps 满帧；② 相机静止时只有 1 关键帧、建不了图；③ ffmpeg 需装 CLI（云上只有 libav* 库）。

**3.7 质量调优（已完成 ✅）：增加训练迭代数**
- [x] 根因：`gaussian_mapper.cpp` 主循环 `if (SLAM_ended_ || getIteration() >= iterations_) break;` —— 视频结束 SLAM 即 shutdown，mapper 提前停（本次仅 3881/30100 迭代 = 12.9%）。
- [x] 已改：去掉 `SLAM_ended_` 短路，让 mapper 在 SLAM 结束后继续训练到 `max_num_iterations`。
- [x] 重编 + 重跑同 500 帧：**30181 迭代，耗时 589s ≈ 9.8 分钟**。
- [x] 质量对比：PSNR **16.82 → 21.94 dB（+5.1）**，高斯数 45,664 → **255,110（5.6×）**，结果在 `results/m3_motion_full/`。
- [x] 结论：离线完整训练 ~10 分钟，与 <10s 在线目标冲突 → 真增量需「每关键帧少量迭代 + 后台精修」双轨，见 M4。

**验收**：✅ **边拍边建全链路跑通**——手机实时流 → 增量建图 → WebSocket 推送 → 浏览器实时渲染（实测 2524 帧 → 30k 高斯，~5s 整幅刷新）。

### M4 — 真增量重建 + 双轨训练 + 增量传输（进行中）

> 目标：把「离线批处理」改造成「在线增量重建 + 增量下发」。
> 核心决策（已定）：
> - **双轨制**：在线轨（新关键帧到达即少量迭代，保低延迟）+ 后台精修轨（空闲时训练到 `max_num_iterations`）。
> - **max_num_iterations 暂定 10000**（项目最后再微调）。
> - **增量传输**：splat delta（新增/更新/删除）+ 周期性全量快照，参考 EAGLES。
> 代码位置：Photo-SLAM 源码在云 `~/Photo-SLAM/src/`（C++，改动需重编）；WebSocket 服务端新增（Python/Node）；Web 客户端在本地 `tools/splat-web/`。

#### 设计要点

1. **真增量输入**：tum_mono 不再一次性 `LoadImages`，改为轮询 `~/live/rgb/` 目录（ffmpeg 持续落盘），新帧一到就 `SLAM.TrackMonocular()`。
2. **双轨训练**：`gaussian_mapper.cpp` 的 `run()` 做优先级调度——有新关键帧先跑 K 次迭代（在线），空闲时跑精修迭代（后台），累计到 10000。两轨共享同一份高斯。
3. **增量导出**：给高斯加稳定 ID，训练中周期 diff（densify→新增、prune→删除、optimize→更新），产出 delta；配周期全量快照保证断线可恢复。
4. **在线延迟目标**：采集→抽帧→跟踪→在线迭代→delta→Web，<10s（后台精修不影响在线帧率）。

#### Step-by-step

**M4.0 参数与质量基线（已完成 ✅）**
- [x] `tum_mono.yaml`：`max_num_iterations` / `densify_until_iter` / `prune_big_point_after_iter` → 10000（本地 + 云已同步）。
- [x] 源码改动归档：`cloud/patches/photoslam_source_patches.patch`（CUDA arch 89、build.sh 重写、`SLAM_ended_` 迭代修复）。
- [x] 离线重跑 500 帧：**10081 迭代，耗时 201s ≈ 3.35 分钟**，结果在 `results/m4_baseline/`。
- [x] PSNR-耗时曲线（性价比结论）：3881→16.82dB/70s，**10081→20.29dB/201s**，30181→21.94dB/589s → **10000 是拐点**（拿 +3.47dB，只花 1/3 时间；再多 3× 时间仅 +1.65dB）。

**M4.1 真增量输入（tum_mono 轮询新帧，已完成 ✅）**
- [x] 定位主程序：`examples/tum_mono.cpp`（非 `src/`），把「一次性 `LoadImages`+固定 for」改为「轮询 `rgb.txt`、有新帧才 `TrackMonocular`、`DONE` 哨兵文件结束」。
- [x] 补丁脚本 `scripts/m4.1_patch_tum_mono.py`、辅助 `scripts/m4.1_feed_frames.py`；源码改动已并入 `cloud/patches/photoslam_source_patches.patch`。
- [x] 验证：空目录启动无报错 → 喂 30 帧地图初始化（241 点）→ 喂 200 帧持续建图（跟踪丢失后重定位）→ `DONE` 触发 `Shutdown`。
- **验收**：✅ 目录增长 = 建图增长。

**M4.2 双轨 mapper（在线快速 + 后台精修，核心已完成 ✅）**
- [x] 架构本身即双轨：`run()` 里 `combineMappingOperations()`（新关键帧在线集成）+ `trainForOneIteration()`（持续精修），两轨共享同一份高斯。
- [x] 在线导出触发：`combineMappingOperations()` 后调 `gaussians_->saveSplatDelta(...)`（新关键帧→立即导出 delta）。
- [x] 后台精修：`SLAM_ended_` 补丁（M3.7）让训练继续到 10000 迭代。
- **验收**：✅ 新关键帧集成后即导出 delta；空闲时持续精修到 10000。

**M4.3 splat 增量导出（核心已完成 ✅，updated/量化留 M4.5）**
- [x] 稳定 ID：`gaussian_model` 加 `gaussian_id_` tensor（int64 单调递增），`createFromPcd` 初始化、`densificationPostfix` 追加、`prunePoints` 同步裁剪 + 记录删除 id。
- [x] delta 导出：`saveSplatDelta()` 写 `deltas/delta_<iter>.txt`（`added`=id>last_max 的新高斯、`removed`=被 prune 的 id）。
- [x] 实测：delta_1=首帧全量 1777，delta_10=+159，delta_1007=+2939/−2119，ID 稳定、增删正确。
- [ ] 留待 M4.5：`updated`（外观精修）delta、fp16/int16 量化、周期全量快照、WebSocket 推送。

**M4.4 WebSocket 推送服务（已完成 ✅）**
- [x] 云端 Node WebSocket server（`scripts/m4_ws_server.js`，`ws` 包，云上 npm 需 `--registry https://registry.npmjs.org`），监控快照 ply 变化即广播二进制。
- [x] 客户端连接时立即回发当前快照（断线重连恢复）。
- [x] Mac SSH 端口转发（`-L 8765:localhost:8765`）+ 本地 `?live=ws://localhost:8765` 接入。
- **验收**：✅ 服务端持续推送（快照 12.5→22.9MB），浏览器收到并渲染。

**M4.5 Web 端动态 splat（核心已完成 ✅，delta 级增量留待）**
- [x] 浏览器 `app.js` 加 `connectLive()`：`?live=ws://…` 下连 WebSocket、收二进制 ply 快照、`display()` 渲染（整幅重载）。
- [x] 无头 Chrome 验证 PASS：`liveConnected` + `firstRender` + `count=92256` + 截图。
- [ ] 留待：delta 级增量增删（改 GS3D 内部 buffer），现为整幅快照重载（~1s）。

#### 风险与对策

| 风险 | 对策 |
|---|---|
| 在线/后台两轨抢 GPU | 在线轨优先（新关键帧插队），后台轨让路 |
| 增量模式下单目跟踪丢失 | 沿用 ORB-SLAM3 重定位+回环；慢速拍摄 |
| delta 体积仍大 | 量化 + 只发变化量 + 阈值过滤 |
| 高斯 ID 在 densify/prune 下不稳定 | 复用 densify 索引管理；删除用 tombstone 复用 |

### M5 — 收尾：质量 + 体验 + 稳定化

- [x] **端到端联调**：实时采集 → 增量建图 → WebSocket → 浏览器实时渲染，~5s 整幅刷新（M4.5 已无头验证 PASS）。
- [~] **真·增量增删**（delta 级改 GS3D buffer）—— **已决定暂缓**：当前 ~5s 整幅刷新体验 OK，等带宽真成瓶颈时再做（见 M5.1）。
- [x] **采集质量（内参）**：真机内参已读+实测验证（fx=892.4），替换了原粗值 fx=1280。剩余：畸变 k1..k5 待棋盘格标定、更高码率（341kbps 仍是瓶颈）、更长更慢扫动。
- [ ] **快照压缩**：参数量化（fp16）把快照从 20MB 压到 ~10MB，提速刷新（暂缓，网速 7MB/s 下 ~5s 刷新够用）。
- [x] **体验（推流按钮）**：浏览器加「开始/停止/重新开始」按钮，采集起点由用户掌控；收到最终结果时提示「已收到完整迭代结果」。
- [~] **稳定性**：SSH 隧道会掉，已用带自动重连循环兜底；**计划更换网络设备 / 加 AP 提高带宽**（根因=全链路挤手机热点一条上行）。
- **验收**：手机拍摄，浏览器 ~5s 内看到场景更新、可旋转缩放（已达成）。

### M5.1 真·增量增删（→ 已升级为 M7，见 `docs/plan_m7.md`）

> 决策（2026-09-24）：当前 ~5s 整幅刷新体验 OK，先不实现；改为**直接换网络设备提高带宽**（更治本，也更利于高码率建模）。
> 更新（2026-09-27）：网络设备到位后，下行整幅重载（~32 Mbps）仍与上行采集争抢同一条热点链路，
> 故把「增量增删」正式立项为 **M7 增量下行推流**，设计、数据格式、step-by-step、验收标准全部见 `docs/plan_m7.md`。

**现状**：整幅快照 ~20MB / 5s 下载 ≈ 32 Mbps 下行。
**目标**：delta（added/removed）+ 周期全量（~15s 兜底），平均下行降到 ~5–10 Mbps。

### M5.2 后台精修结果下发 + 自动下载

- [x] 现状：后台精修（尾训练到 10000）每 300 迭代的**中间快照会推浏览器**（WS 监控 snapshot.ply）。
- [x] 最终结果下发：`savePly` 后把最终版也写到 `live/snapshot.ply`（C++ 补丁），WS 推浏览器显示最终版（实测最终 ply 与 snapshot 高斯数一致 = 71744）。
- [x] 浏览器提示：WS 检测到 tum_mono 退出后发 `final` 消息，浏览器显示「已收到完整迭代结果」。
- [x] 自动下载：`scripts/m5_auto_download.sh` 轮询最终 ply → 稳定后下载到 `results/<run>_<时间戳>/`（带时间戳避免覆盖）。

### M5.3 建模参数与精度提升优先级

当前参数：1280×720 @ 15fps、IP Webcam 质量 70、**15000 迭代**（2026-09-27 由 10000 上调）、fx=892.4（真值）、k=0（未标定）、SH=3。

**已实测（2026-09-24）**：quality 70 + 正确内参 + 干净起点 → **PSNR 25.97 dB**（对比 q50 + 粗内参 19.33 dB，+6.64 dB）。

提升顺序（按性价比）：
1. **带宽 → 输入码率**（IP Webcam 质量 70→80+，或 1080p）：最大杠杆。**计划先换网络设备提高带宽，再继续加码率**。
2. **采集 → 更长更慢扫动**（更多视差 + 回环）：几何精度。
3. **标定 → 畸变 k1..k5**（棋盘格）：几何精度。
4. **算力 → 迭代数**（10000→30100）：+1.65 dB（实测），但时间 ×3。
5. **算力 → SH 阶数 / 高斯密度**：细节。

---

### M6 — 取景/构图推荐（🆕 独立计划，见 `docs/plan_m6.md`）

> M6 的完整设计、step-by-step、自检/验收门禁、问题复查记录**已整体迁移到 `docs/plan_m6.md`**（原 `m6_viewpoint_recommendation.md`）。
> 现状速览：M6.0（地基 + 除雾 + 浏览器交付）、M6.1（上方向）、M6.2（候选生成）已完成；M6.3 L1 排序、M6.4 VLM 反馈、M6.5 美学场蒸馏待做。

---

### M7 — 增量下行推流（🆕 独立计划，见 `docs/plan_m7.md`）

> 把「整幅 ply 快照重载（~32 Mbps 下行）」改成「高斯增量增删 + 周期全量兜底」，降低浏览器下行带宽、
> 让上行采集能吃更高码率。设计、delta 数据格式、step-by-step（M7.1 C++ 序列化 / M7.2 WS delta 推送 / M7.3 浏览器累计重建）、
> 验收标准、与 M6 的先后依赖，全部见 `docs/plan_m7.md`。
> 现状速览：**Phase A 已完成并部署**。M7.1（C++ 序列化，`full_*.bin` 重建与 `snapshot.ply` 逐字节一致）、M7.2（Node delta 推送 + 跨采集迭代回退检测）、M7.3（浏览器累计重建）全部上线并验证；新增 M7.4（原子写 + 防误归档 + 迭代号显示）、M7.5（丢跟踪 15s 自动重置重新初始化 + ORB 阈值调优）。下一步真机实测 + 可选 Phase B（GS3D 原地 buffer）。

---

## 6. 风险与对策（单目永久化后的重点）

| 风险 | 对策 |
|---|---|
| **单目尺度漂移**（场景真实尺寸不准、越走越偏） | 先接受（toy）；后期加 Depth Anything v2-S 作尺度先验，或手机 IMU（VINGS-Mono）。**相关提案（未采纳，勿实施）**见 `docs/proposal_arcore_vio.md` |
| 快速运动丢跟踪 | 用 Photo-SLAM 的 ORB-SLAM3 重定位；拍摄时放慢、回环扫 |
| 动态物体（行人）ghost | 后续加语义 mask 剔除 |
| 4090 下 torch/cuda 版本冲突 | 统一 torch 2.x + cu118/cu121，独立 conda 环境隔离 |
| 高斯数膨胀 OOM | 设高斯数上限、控制 SH 阶数、限分辨率 |
| 手机与云跨网穿透 | 主用 Mac SSH 反隧道；长期用自建 frp（已放弃 Tailscale） |

---

## 7. 交付物索引

- 总体计划：`docs/plan.md`（本文件）
- 🆕 取景/构图推荐（M6）独立计划：`docs/plan_m6.md`
- 🆕 增量下行推流（M7）独立计划：`docs/plan_m7.md`
- 部署选型：`docs/gs_slam_deployment.md`
- 镜像重建清单：`docs/monoGS_4090_image_checklist.md`、`docs/photoslam_4090_image_checklist.md`
- 排障记录：`docs/m2_photoslam_handoff.md`
- Photo-SLAM 源码补丁归档：`cloud/patches/photoslam_source_patches.patch`（CUDA arch 89、build.sh 重写、`SLAM_ended_` 迭代修复；**权威版，2026-09-28 从云实例重新生成**）
  - 重建指南：`build_guide.md`；云实例资产总览：`cloud/README.md`
  - ⚠️ `cloud/patches/superseded/photoslam_source_patches.20260924.patch` 是 **M4 时代的过期版本**（缺 M5.2/M7.1/M7.5 改动），仅供历史对比，**勿用于重建**
- ⚠️ **提案·未采纳·未实施**：`docs/proposal_arcore_vio.md`（用 ARCore VIO 外部给定位姿以替代单目 SLAM 位姿）。**仅记录设计思路，当前路线不变，勿据此实施。**
- 浏览器 3DGS 查看器：`docs/splat_web_viewer_usage.md`、`tools/`
- 云实例脚本：`scripts/setup_monogs_4090.sh`、`scripts/run_photoslam.sh`、`scripts/m3_pull_rtsp.sh`
- 相机配置：`photo_slam_config/phone_720p.yaml`（M2 竖屏）、`tum_mono.yaml`（gaussian mapper）、`tum_freiburg1_desk.yaml`（参考模板）
- 渲染原理 demo：`gaussian_splatting_demo.py`（已完成，对应 Web 渲染那段的数学）
