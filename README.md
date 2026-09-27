# ViewFinder

> 手机实时视频 → 云端 3D Gaussian Splatting 在线重建 → 浏览器实时渲染 + 取景/构图推荐。
> 性质：个人兴趣项目，toy demo。

```
Android 手机 (IP Webcam, 1280×720 RTSP)
        │  WLAN
        ▼
Mac ──(反隧道 -R 18080)──► 云 RTX 4090 ── Photo-SLAM 增量建图（单目）
        │                                    │  deltas/*.bin + live/snapshot.ply
        │                                    ▼
        │                        Node WS+HTTP 服务 (:8765)  ── 归档 / 除雾 / 取景推荐
        ▼                                    │
Mac:8765 ──(正隧道 -L 8765)──► 浏览器 http://127.0.0.1:8099/?live=ws://localhost:8765
```

---

## 🚨 我要换云实例 / 重建环境

**读 [`build_guide.md`](build_guide.md)。**

它是在**没有当前实例镜像**的前提下，从零重建整套系统（云实例 + Mac 侧）的完整手册：
实例选型 → apt → OpenCV+CUDA → LibTorch → Photo-SLAM 打补丁编译 → conda+MonoGS → Node → 部署 → 密钥 → 端到端验收，
外加 29 条实际踩过的坑和一份版本锁定表。

云实例专属资产（只活在实例上的那些）总览见 [`cloud/README.md`](cloud/README.md)。

---

## 📖 我要日常使用

**读 [`docs/full_pipeline_guide.md`](docs/full_pipeline_guide.md)。**
一键脚本、实时采集流程、case 管理、取景推荐、本地视频诊断、常见问题，全在里面。

```bash
bash scripts/vf_up.sh        # 一键拉起：反隧道 + 正隧道 + 云 node 服务 + 健康检查
bash scripts/vf_status.sh    # 全链路体检
```

浏览器开：`http://127.0.0.1:8099/?live=ws://localhost:8765`

---

## 首次使用（Mac 侧）

```bash
# 1) 配置（.env 被 gitignore，绝不提交）
cp .env.example .env && chmod 600 .env
#    填入：viewfinder_host / viewfinder_port / viewfinder_username / viewfinder_password
#          ip_webcam_lan_ip（手机 IP Webcam 的局域网地址）

# 2) 起本地 3DGS 查看器（零依赖 Node 静态服务）
./tools/open-web-viewer.command          # → http://127.0.0.1:8099/

# 3) 把云侧脚本同步到实例（改了 scripts/ 或 m6/ 后要重跑）
bash scripts/vf_deploy.sh

# 4) 起全链路
bash scripts/vf_up.sh
```

> 云侧还需要一次性手工建 `/root/m6/.env`（`DEEPSEEK_KEY=...`），它**不随 `vf_deploy.sh` 同步**。
> 详见 `build_guide.md` §3 Phase H。

---

## 仓库结构

```
ViewFinder/
├── build_guide.md          ← 🚨 换实例 / 灾难恢复手册
├── VERSIONS.md             ← 版本锁定表（含核对命令）
├── .env.example            ← 配置模板
├── docs/                   ← 计划、操作指南、镜像清单、内参方案
├── cloud/                  ← 云实例专属资产（补丁 / 构建脚本 / 版本快照）
├── scripts/                ← Mac 编排（vf_*.sh）+ 云侧脚本源
├── m6/                     ← M6 取景推荐流水线（含 abl/ A/B 配置）
├── photo_slam_config/      ← 相机内参与训练参数（云端的源）
├── tools/                  ← 本地 3DGS 查看器（浏览器版 + Brush）
├── 3DGS_demo/              ← 高斯泼溅原理 demo
└── archive/                ← 已归档的选型与排障记录
```

**哪些东西不入库**：`.env`（密钥）、`sample_videos/`、`results/`（训练产物）、
`tools/{_downloads,_captures,_smoke,_venv,platform-tools,Brush.app,brush}/`、
`tools/splat-web/{node_modules,.npm-cache}`、两个 25 MB 级 demo ply。
每项的重新获取方式见 `build_guide.md` §2.2 与 `.gitignore` 内注释。

---

## 关键文档

| 文档 | 内容 |
|---|---|
| [`build_guide.md`](build_guide.md) | **从零重建整套系统**（换实例 / 灾难恢复） |
| [`docs/full_pipeline_guide.md`](docs/full_pipeline_guide.md) | 日常操作：一键脚本、采集流程、case、取景推荐 |
| [`docs/plan.md`](docs/plan.md) | 总体方案与实施计划（M0–M5） |
| [`docs/plan_m6.md`](docs/plan_m6.md) | M6 取景 / 构图推荐 |
| [`docs/plan_m7.md`](docs/plan_m7.md) | M7 增量 3DGS 下行推流 |
| [`docs/operations_guide.md`](docs/operations_guide.md) | 拉镜像 → 开启实时建图（上一版重建指南，已被 build_guide 取代） |
| [`docs/photoslam_4090_image_checklist.md`](docs/photoslam_4090_image_checklist.md) | Photo-SLAM 云镜像重建清单（编译踩坑） |
| [`docs/monoGS_4090_image_checklist.md`](docs/monoGS_4090_image_checklist.md) | MonoGS 镜像清单（M1 冒烟） |
| [`docs/android_camera_intrinsics.md`](docs/android_camera_intrinsics.md) | 手机内参读取与换算 |
| [`docs/splat_web_viewer_usage.md`](docs/splat_web_viewer_usage.md) | 浏览器查看器参数总表与排查 |
| [`cloud/README.md`](cloud/README.md) | 云实例资产总览 + 「哪个文件才是权威」 |
| [`tools/README.md`](tools/README.md) | 本地查看器（浏览器版 / Brush）安装与用法 |
| [`VERSIONS.md`](VERSIONS.md) | 版本锁定表 + 核对命令 |
| [`archive/`](archive/) | 已归档：选型对比、M2 排障记录、未采纳提案 |

---

## 三条硬约束（换环境时最容易翻车）

1. **云实例家目录必须是 `/root`，conda 环境必须叫 `myconda`**
   —— `m6/render_pose.py`、`m6/*.sh`、`m6/baseline_stats.py` 里全是硬编码路径。
2. **C++ ABI 必须是新 ABI**：用 **cxx11-abi 版 LibTorch 2.1.2** + 系统默认新 ABI 的 OpenCV/jsoncpp/boost。
   ⚠️ 「conda 旧 ABI torch + 逐个重编依赖」是**已实测证实的死路**。
3. **CUDA 架构必须是 sm_89**（`CUDA_ARCHITECTURES "89"`），上游硬编码的 `"75;86"` 在 4090 跑不了。

详见 `build_guide.md` §0.2。

---

## 当前状态（2026-09-28）

| 里程碑 | 状态 |
|---|---|
| M0 环境就绪 | ✅ |
| M1 MonoGS 冒烟 | ✅ |
| M2 Photo-SLAM 离线跑通 | ✅ |
| M3 实时流 | ✅ |
| M4 增量 + 双轨训练 + 增量传输 | ✅ |
| M5 收尾（质量 / 体验 / 稳定化） | 🔄 进行中 |
| M6 取景 / 构图推荐 | 🔄 M6.0–M6.2 完成 |
| M7 增量下行推流 | ✅ Phase A 已完成并部署 |

**重建质量**：PSNR **25.97 dB**（真值内参 `fx=892.4` + IP Webcam quality 70 + 干净起点）。

**已知限制**：单目尺度漂移（接受，toy）；畸变 k1..k5 未标定；上行带宽受手机热点限制。

---

## 上游依赖

| 组件 | 仓库 | 锁定 commit |
|---|---|---|
| Photo-SLAM | `HuajianUP/Photo-SLAM` | `f8bfb2f` + 本仓库补丁 |
| MonoGS | `muskie82/MonoGS` | `6c9254c` |
| GaussianSplats3D | `@mkkellogg/gaussian-splats-3d` | `^0.4.7` |
| three.js | `three` | `^0.186.0` |

> ⚠️ 本仓库**不 vendor** Photo-SLAM / MonoGS 源码（ORB-SLAM3 词袋单文件 145 MB）。
> 改用「上游精确基线 + 补丁 + 变更文件完整副本」三重保险，详见 `cloud/README.md`。
