# ViewFinder 重新部署指南（build_guide.md）

> **目标**：在一台**全新的云 GPU 实例**（没有当前实例的镜像）和一台**新 Mac**上，仅凭本 Git 仓库，
> 从零重建出完整可运行的 ViewFinder 全链路：手机实时流 → 云端 Photo-SLAM 增量建图 → WebSocket 下行 → 浏览器实时渲染 + case 归档/除雾 + 取景推荐。
>
> **配套文档**：全链路日常操作见 `docs/full_pipeline_guide.md`；总体方案见 `docs/plan.md`；M6/M7 见 `docs/plan_m6.md` / `docs/plan_m7.md`。
>
> **本文档的性质**：它是「**灾难恢复 / 迁移**」手册，不是日常使用手册。
> 日常使用（一键脚本、case 管理）请用 `docs/full_pipeline_guide.md`。

---

## 0. 总览

### 0.1 重建后的目标状态

| 环节 | 组件 | 位置 |
|---|---|---|
| 采集 | 手机 IP Webcam（1280×720 RTSP） | 手机 |
| 打通 | 反隧道 云:18080 → 手机:8080；正隧道 Mac:8765 → 云:8765 | Mac |
| 建图 | Photo-SLAM `bin/tum_mono`（含 M4/M5/M7 补丁） | 云 `/root/Photo-SLAM` |
| 拉流 | `m3_pull_rtsp.sh`（ffmpeg 抽帧落盘 + 真实 mtime 时间戳） | 云 `/root/` |
| 起停 | `m4_start.sh` / `m4_stop.sh` | 云 `/root/` |
| 下行 | Node WS+HTTP 服务（`server.js` + `m7_ws_delta.js`，端口 8765） | 云 `/root/m4_ws/` |
| 归档/除雾 | `finalize_case.sh` / `auto_dehaze.sh` + `m6/*.py` | 云 `/root/m6/` |
| 取景推荐 | `framing_for_case.sh` → candidate_gen2 → screen → vlm_score | 云 `/root/m6/` |
| 展示 | 本地静态查看器 + WebGL2（GaussianSplats3D / three.js） | Mac `tools/splat-web/dist/` |
| 编辑器 | Brush.app（原生 Metal，可选） | Mac `tools/` |

### 0.2 三条硬约束（**违反任何一条都会失败**）

1. **路径必须一致**：云上必须以 `/root` 为家目录，conda 环境必须叫 `myconda` 且位于 `/root/miniconda3/envs/myconda`。
   > 原因：`m6/*.py`、`m6/*.sh` 里**硬编码**了 `/root/m6`、`/root/MonoGS`、`/root/miniconda3/envs/myconda/bin/python`、`/root/live`。
   > 详见 §8 附录对照表。
2. **C++ ABI 必须统一为新 ABI**：依赖 **cxx11-abi 版 LibTorch 2.1.2** + **系统默认新 ABI** 的 OpenCV/jsoncpp/boost。
   > ⚠️ **严禁**走「conda 旧 ABI torch + 把所有 C++ 依赖重编成旧 ABI」的路线（`cloud/legacy/rebuild_abi.sh`）。
   > 那是一条**已实测证实的死路**（会在 boost 上卡死），保留该脚本仅为历史记录。
3. **CUDA 架构必须是 sm_89**：`CMakeLists.txt` 里 `CUDA_ARCHITECTURES` 必须为 `"89"`（上游硬编码是 `"75;86"`，4090 跑不了）。

### 0.3 ✅ 前置提取：**已在当前实例存活时完成**（2026-09-28）

**这是整件事的关键。** 云实例上有相当一部分东西**只存在于实例上**，本地仓库里没有（或已有副本但**已过期**）。
一旦实例释放，这些内容就永久丢失。

> **状态：已全部提取完毕**，产物在 `cloud/`（约 488 KB）+ `m6/abl/` + `docs/operations_guide.md`。
> 下表保留为**审计记录**：说明抓了什么、为什么、以及是否已验证。

| # | 必须提取 | 提取前状态 | 目标位置 | 结果 |
|---|---|---|---|---|
| 1 | **Photo-SLAM 的真实源码补丁** | 🔴 `docs/photoslam_source_patches.patch` **已过期**：缺 M5.2（最终快照下沉 live/）、M7.1（`saveSplatFull` 二进制全量）、M7.5（丢跟踪日志 + 重初始化兜底 + `TUM_PACE_MS`）；且 `tum_mono.yaml` 写的是 10000/无 `opacity_reset_interval`，**实际是 15000/3000** | `cloud/patches/photoslam_source_patches.patch` | ✅ 553 行 / 24497 B，sha256 `a014e95a…`；过期版移入 `cloud/patches/superseded/` |
| 2 | 补丁涉及文件的**完整副本**（防上游变更/失联的兜底） | ❌ 无 | `cloud/patches/modified_files/` | ✅ 10 个文件（7 改动 + 3 新增） |
| 3 | **云侧环境构建脚本**（只在云上有） | ❌ 不在仓库 | `cloud/setup/`（8 个）+ `cloud/legacy/`（1 个） | ✅ `rebuild_abi.sh` 属废弃路线，归入 `legacy/` |
| 4 | **云版** patch 脚本 `m7.1_*` / `m7.5_*` / `m7_rebuild_check` | 🟡 本地有同名文件但内容不一致 | `cloud/upgrade/` | ✅ 已抓；⚠️ **但它们是历史残留、与云端源码不一致，不可用于重建**（见下方§） |
| 5 | `m4_ws_server.py`（早期 Python 版 WS 服务，历史参考） | ❌ 不在仓库 | `cloud/legacy/` | ✅ |
| 6 | **环境版本快照**（pip freeze / apt-mark / dpkg / 各工具版本） | ❌ 不在仓库 | `cloud/env/` | ✅ 6 个文件，1935 行 |
| 7 | **`/root/m6/abl/`**（A/B 实验配置：`tum_mono_tuned.yaml`、`cfg/tum_mono.yaml`、`cfg/nopyr.yaml`） | ❌ 不在仓库；且 `vf_deploy.sh` 只同步 `m6/*.sh` 与 `m6/*.py`，**这个子目录永远不会被同步** | `m6/abl/` | ✅ 12 KB |
| 8 | `/root/Photo-SLAM/.m7_backup_20260927_020013/`（M7.1 补丁前的 3 个源文件备份，历史参考） | ❌ 不在仓库 | `cloud/patches/pre_m71_backup/` | ✅ |
| 9 | `.tmp/installation_guide.md`（**上一版重建指南**） | 🟠 在 **`.tmp/`（已被 .gitignore 排除）**里，随时会被清掉 | `docs/operations_guide.md` | ✅ 已提升（原件保留在 `.tmp/`） |
| 10 | 密钥清单（`m6/.env` 的 `DEEPSEEK_KEY`；Mac `.env`） | ❌ 故意不入库，但**必须另行记录** | 密码管理器 + `.env.example` | ⬜ **待办**（见 Phase H） |

> **已核对：以下文件无需额外提取**（本地已有同哈希副本，列在 `modified_files/` 里只作校验锚点）：
> - `run_photoslam.sh` ≡ `scripts/run_photoslam.sh`
> - `cfg/ORB_SLAM3/Monocular/TUM/phone_720p.yaml` ≡ `photo_slam_config/phone_720p.yaml`
> - `cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml` ≡ `photo_slam_config/phone_1280x720.yaml`
> - `cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml` ≡ `photo_slam_config/tum_mono.yaml`

#### ⚠️ 提取中发现的陷阱：`cloud/upgrade/*.py` 与云端源码不一致

抓取时逐文件哈希比对发现：**`upgrade/` 里的 patch 脚本落后于云端实际源码**，
两者不可互相替代 —— **重建只信 `cloud/patches/`**。

| 脚本 | 脚本会生成的行为 | 云端**实际源码** | 判定 |
|---|---|---|---|
| `m7.1_patch_save_splat_delta.py` | 直接写 `delta_<iter>.bin` | **有**原子改名（`.tmp` → 正式名） | 脚本**落后于源码** |
| `m7.5_patch_tum_mono_reset.py` | 生成 `lostSinceT` / `kLostResetAfterS=15.0`「丢跟踪 15s 重置」 | 这对变量**已声明但从未使用（死代码）**；实际生效的是状态日志 + `NOT_INITIALIZED>30s → ResetActiveMap()` | 脚本生成的是**已被否决的旧方案** |

> 云端源码 `examples/tum_mono.cpp` 第 117–118 行残留着那段死代码，
> 证明实例是**用旧版脚本打的补丁**，之后源码被手工演进到新方案，但从未用新脚本重打（重打需重编，成本高且功能无影响）。
> 这段死代码只产生一个编译警告，**不影响功能**；`modified_files/examples/tum_mono.cpp` 忠实保留了它（即云端真实现状）。

**提取命令（已执行，保留供复核）**：

```bash
cd /path/to/ViewFinder
source scripts/vf_env.sh

# 1) 权威补丁（覆盖过期的本地副本）
vf_ssh 'cd /root/Photo-SLAM && git diff > /tmp/photoslam_source_patches.patch'
mkdir -p cloud/patches/modified_files
vf_scp "$VF_USER@$VF_HOST:/tmp/photoslam_source_patches.patch" cloud/patches/

# 2) 全部变更文件的完整副本（7 个已跟踪改动 + 3 个未跟踪文件）
for f in CMakeLists.txt build.sh cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml \
         examples/tum_mono.cpp include/gaussian_model.h src/gaussian_mapper.cpp src/gaussian_model.cpp \
         cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml cfg/ORB_SLAM3/Monocular/TUM/phone_720p.yaml \
         run_photoslam.sh; do
  mkdir -p "cloud/patches/modified_files/$(dirname "$f")"
  vf_scp "$VF_USER@$VF_HOST:/root/Photo-SLAM/$f" "cloud/patches/modified_files/$f"
done

# 3) 云侧构建脚本（rebuild_abi.sh 属废弃路线，归入 legacy/）
mkdir -p cloud/setup cloud/upgrade cloud/legacy cloud/env
for f in apt_deps.sh install_deps_b.sh install_small_b.sh build_opencv_cuda.sh \
         build_photoslam.sh run_slam_b.sh compile_b2.sh compile_monogs_b.sh; do
  vf_scp "$VF_USER@$VF_HOST:/root/$f" cloud/setup/
done
vf_scp "$VF_USER@$VF_HOST:/root/rebuild_abi.sh"    cloud/legacy/
vf_scp "$VF_USER@$VF_HOST:/root/m4_ws_server.py"   cloud/legacy/

# 3b) m6/abl（A/B 实验配置，vf_deploy.sh 不会同步它）
mkdir -p m6/abl/cfg
vf_scp -r "$VF_USER@$VF_HOST:/root/m6/abl/." m6/abl/

# 3c) M7.1 补丁前的源文件备份（可选，历史参考）
mkdir -p cloud/patches/pre_m71_backup
vf_scp -r "$VF_USER@$VF_HOST:/root/Photo-SLAM/.m7_backup_20260927_020013/." cloud/patches/pre_m71_backup/

# 4) 云版 patch 脚本（历史残留；与云端源码不一致，勿用于重建）
vf_scp "$VF_USER@$VF_HOST:/root/m7.1_patch_save_splat_delta.py" cloud/upgrade/
vf_scp "$VF_USER@$VF_HOST:/root/m7.5_patch_tum_mono_reset.py"    cloud/upgrade/
vf_scp "$VF_USER@$VF_HOST:/root/m7_rebuild_check.py"             cloud/upgrade/

# 5) 环境版本快照
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

# 6) 提升差点被忽略的重建指南
cp .tmp/installation_guide.md docs/operations_guide.md

# 7) 停用过期补丁：移出 docs/ 的"happy path"，避免误用
mkdir -p cloud/patches/superseded
mv docs/photoslam_source_patches.patch \
   cloud/patches/superseded/photoslam_source_patches.20260924.patch
```

> ⚠️ 注意第 5 步的 `nvcc` **必须写全路径** `/usr/local/cuda-12.1/bin/nvcc`：
> 非交互式 SSH 的 `PATH` 里没有 CUDA，直接 `nvcc` 会 `command not found`（实测踩到过）。

**提取后必须校验**（这条校验决定后面能不能顺利重建）—— ✅ **已通过**：

```bash
# 方式 A（Mac 能连 GitHub 时）
cd /tmp && rm -rf ps-verify && git clone https://github.com/HuajianUP/Photo-SLAM.git ps-verify
cd ps-verify && git checkout f8bfb2f
git apply --check "$REPO/cloud/patches/photoslam_source_patches.patch"
```

```bash
# 方式 B（Mac 连不上 GitHub 时——实测本机就是这种情况）：在云实例上做，且更强——
# 同时验证「补丁能应用」+「打出来的文件与抓取的副本逐字节一致」。
# 用 git archive 导出纯净树，完全不碰实例的工作区。
cd /path/to/ViewFinder && source scripts/vf_env.sh
vf_scp cloud/patches/photoslam_source_patches.patch "$VF_USER@$VF_HOST:/tmp/ps.patch"
vf_ssh 'rm -rf /tmp/mf && mkdir -p /tmp/mf'
vf_scp -r cloud/patches/modified_files/. "$VF_USER@$VF_HOST:/tmp/mf/"
vf_ssh 'set -e
  cd /root/Photo-SLAM
  rm -rf /tmp/ps-pristine && mkdir -p /tmp/ps-pristine
  git archive f8bfb2f | tar -x -C /tmp/ps-pristine        # 导出上游纯净树（473 文件）
  cd /tmp/ps-pristine
  git apply --check /tmp/ps.patch                         # ① 能否干净应用
  git apply /tmp/ps.patch
  for f in CMakeLists.txt build.sh cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml \
           examples/tum_mono.cpp include/gaussian_model.h src/gaussian_mapper.cpp src/gaussian_model.cpp; do
    cmp -s "/tmp/ps-pristine/$f" "/tmp/mf/$f" && echo "OK  $f" || echo "DIFF $f"
  done
  rm -rf /tmp/ps-pristine /tmp/mf /tmp/ps.patch'          # ② 打补丁结果 vs 抓取副本
```

**实测结果（2026-09-28）**：

```
① git apply --check …              → ✅ PATCH APPLIES CLEANLY to f8bfb2f
② 逐文件 cmp 打补丁结果 vs modified_files/
   CMakeLists.txt                                    ✅
   build.sh                                          ✅
   cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml   ✅
   examples/tum_mono.cpp                             ✅
   include/gaussian_model.h                          ✅
   src/gaussian_mapper.cpp                           ✅
   src/gaussian_model.cpp                            ✅
   → 7/7 逐字节一致

③ phone_1280x720.yaml / phone_720p.yaml / run_photoslam.sh 上游不存在 → 属预期，
   由 modified_files/ 单独提供（补丁不含新增文件）
```

**结论：补丁 + 基线 commit `f8bfb2f` 足以精确复现云端源码**；`modified_files/` 是不依赖上游仓库的第二条独立路径。

---

## 1. 系统架构

```
手机 (IP Webcam, 1280x720 RTSP)
   │  WLAN
   ▼
Mac ──(反隧道 -R 18080:手机:8080)──► 云实例:18080 ──► m3_pull_rtsp.sh (ffmpeg 抽帧)
   │                                                    │
   │                                                    ▼
   │                                        m4_start.sh ─► tum_mono (Photo-SLAM 增量建图)
   │                                                    │  deltas/*.bin + live/snapshot.ply
   │                                                    ▼
   │                                        node server.js (:8765, WS + HTTP)
   │                                                    │  归档 + 除雾 + 取景推荐
   ▼                                                    ▼
Mac:8765 ──(正隧道 -L 8765)──► 浏览器 http://127.0.0.1:8099/?live=ws://localhost:8765
```

**数据流（M7 Phase A）**：云端 `saveSplatDelta()` 写「新增（id + 62×f32）/ 删除（id）」→
Node 读 delta → WS 二进制（首字节 `0x01`=delta / `0x00`=全量）→ 浏览器累计表按 id 增删 → 本地重建 PLY → 渲染。

### 1.1 组件 ↔ 仓库来源 ↔ 重建方式

| 组件 | 仓库来源 | 重建方式 |
|---|---|---|
| Photo-SLAM 源码 | `cloud/patches/`（补丁）+ 上游 `HuajianUP/Photo-SLAM@f8bfb2f` | clone + `git apply` + `bash build.sh` |
| LibTorch 2.1.2 cxx11-abi | 官方 CDN（不入库，3.9 GB） | `curl` 下载 |
| OpenCV 4.10.0 + CUDA | 上游源码（不入库） | `cloud/setup/build_opencv_cuda.sh` |
| MonoGS（M6 渲染依赖） | 上游 `muskie82/MonoGS@6c9254c` | clone + submodule + `pip install -e` |
| conda `myconda` | `cloud/env/myconda.requirements.txt` | `conda create` + `pip install -r` |
| 云侧运行脚本 | `scripts/`、`m6/`（**本地即源**） | `bash scripts/vf_deploy.sh` |
| 相机/训练配置 | `photo_slam_config/`（**本地即源**） | 同上 |
| Mac 查看器 | `tools/serve-splat-web.mjs`、`tools/splat-web/dist/`（**手写源码，非构建产物**） | 直接使用 |
| Brush.app（可选） | 第三方发行版（不入库，145 MB） | 下载 + sha256 校验 |

---

## 2. 仓库内容边界

### 2.1 入库（Git 跟踪）

```
ViewFinder/
├── build_guide.md                 ← 本文档
├── README.md                      ← 入口（指向 build_guide / docs）
├── VERSIONS.md                    ← 版本锁定表（§7 的权威版，含核对命令）
├── .env.example                   ← 配置模板（无真实值）
├── .gitignore
├── docs/                          ← 计划、操作指南、镜像清单、源码补丁说明
│   ├── plan.md / plan_m6.md / plan_m7.md
│   ├── full_pipeline_guide.md     ← 日常操作
│   ├── operations_guide.md        ← 由 .tmp/installation_guide.md 提升而来
│   ├── photoslam_4090_image_checklist.md / monoGS_4090_image_checklist.md
│   ├── android_camera_intrinsics.md / splat_web_viewer_usage.md
│   └── archive/                   ← gs_slam_deployment / m2_photoslam_handoff / proposal_arcore_vio
├── cloud/                         ← 【云实例专属资产，务必入库】（约 488 KB）
│   ├── README.md                  ← 云端资产总览 + 「哪个文件才是权威」警告
│   ├── patches/
│   │   ├── photoslam_source_patches.patch   ← 【权威】7 文件完整 diff（553 行）
│   │   ├── modified_files/                  ← 【权威】变更文件完整副本（10 个，不依赖上游的兜底）
│   │   ├── pre_m71_backup/                  ← M7.1 补丁前的 3 个源文件
│   │   └── superseded/                      ← 过期补丁（M4 时代），仅供历史对比
│   ├── setup/                     ← 云环境构建脚本（8 个）
│   ├── env/                       ← 版本快照（requirements / apt / dpkg / 工具版本）
│   ├── legacy/                    ← 废弃路线留档（rebuild_abi.sh、m4_ws_server.py）
│   └── upgrade/                   ← 云版 patch 脚本（⚠️ 历史残留，勿用于重建）
├── scripts/                       ← Mac 编排（vf_*.sh）+ 云侧脚本源 + 分析脚本
├── m6/                            ← M6 流水线源码（**本地即云端的源**，含 __pycache__ 除外）
│   ├── *.sh / *.py
│   └── abl/                       ← A/B 实验配置（tum_mono_tuned.yaml、cfg/*.yaml）
├── photo_slam_config/             ← 相机内参 + 训练参数（**本地即云端的源**）
└── tools/
    ├── serve-splat-web.mjs / open-web-viewer.command / verify-web-viewer.mjs
    ├── README.md / probe_video.py / audit_recon_quality.py
    └── splat-web/dist/{app.js, index.html, vendor/}   ← 手写源码，必须入库
```

### 2.2 不入库（`.gitignore`）——及重新获取方式

| 排除项 | 体积 | 重新获取方式 |
|---|---|---|
| `.env` | 小 | **手工重建**（见 §3 Phase H / §4.4），值记录在密码管理器 |
| `sample_videos/` | 729 MB | 自行录制，或保留 `m6_test_video.mp4`（13 MB，建议入库作测试基准） |
| `results/` | 198 MB | 训练产物，可再生 |
| `.tmp/` | 小 | 临时草稿；**注意：`installation_guide.md` 必须先移出** |
| `tools/_downloads/` | 107 MB | 重新下载（URL + sha256 见 §4.3） |
| `tools/_captures/` | 15 MB | `dumpsys media.camera` 原始 dump；可选，用 `scripts/android_intrinsics.py` 重取 |
| `tools/_build/` | 1.4 MB | Brush.app 包装脚本 —— 🟡 **建议入库**（体积小、不可再生） |
| `tools/_smoke/` | 37 MB | 无头 Chrome 截图产物，可再生 |
| `tools/platform-tools/` | — | Android SDK platform-tools（Google 官方下载） |
| `tools/Brush.app/` `tools/brush/` | 145 MB | GitHub Release 下载 + sha256 校验 |
| `tools/splat-web/node_modules/` | 26 MB | `cd tools/splat-web && npm i` |
| `tools/splat-web/.npm-cache/` | 6 MB | 缓存，无需保留 |
| `tools/splat-web/dist/splats/*.ply`（大件） | 53 MB | 🟡 建议**只入库 `default.ply`（4.4 MB）** 保证开箱可看；25 MB 的 `m5_*`/`m6_*` 排除 |
| `3DGS_demo/.venv/` `.mplcache/` | 122 MB | `python -m venv` + 装 numpy/matplotlib |
| `__pycache__/`、`.DS_Store`、`*.log` | — | 生成物 |

### 2.3 密钥与凭据（**绝不入库**）

| 位置 | 内容 | 处理 |
|---|---|---|
| `ViewFinder/.env` | 矩池云 host/port/username/password、手机 LAN IP、DeepSeek key | `.env.example` 入库，真值手工填 |
| 云 `/root/m6/.env` | `DEEPSEEK_KEY`（取景推荐的 VLM 调用） | **不随 `vf_deploy.sh` 同步**，重建后手工写入 |
| 云 `/root/m6/abl/` | 取景推荐 A/B 实验数据 | 视需要单独拉取 |

---

## 3. 云实例重建（Phase 0 → I）

> **适用前提**：全新实例，**无当前镜像**，Ubuntu 22.04 + RTX 4090。
> **预计耗时**：apt 5 min；OpenCV+CUDA 编译 40–70 min；LibTorch 下载 15–60 min（看网络）；Photo-SLAM 编译 15–25 min；conda 环境 10–20 min；MonoGS 编译 5–10 min。**合计约 2–3.5 小时**（不含数据迁移）。

### Phase 0 — 实例选型（硬性要求）

| 项 | 要求 | 当前实测值 |
|---|---|---|
| GPU | RTX 4090（sm_89） | NVIDIA GeForce RTX 4090 |
| 驱动 | ≥ 525 | 580.126.20 |
| 系统 | **Ubuntu 22.04** | Ubuntu 22.04 (Jammy) |
| CUDA 工具链 | **nvcc 12.1**，位于 `/usr/local/cuda-12.1` | 12.1.105（`/usr/local/cuda`、`/usr/local/cuda-12` 均软链到它） |
| 磁盘 | ≥ 120 GB（含 47 GB 历史 results 则需更多） | 200 GB，已用 87 GB |
| 家目录 | **必须是 `/root`** | `/root` |
| 公网/内网穿透 | 需要能被 Mac SSH 到（云厂商给 host:port） | 见 `.env` 的 `viewfinder_host` / `viewfinder_port` |

```bash
# 入场自检
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
nvcc --version | tail -2          # 期望 release 12.1
ls -d /usr/local/cuda-12.1
df -h / | tail -1
whoami && echo "HOME=$HOME"       # 必须是 root 且 /root
```

### Phase A — 基础系统依赖

```bash
# cloud/setup/install_deps_b.sh + install_small_b.sh + apt_deps.sh 的合并结果
apt-get update && apt-get install -y \
  cmake build-essential gcc g++ gfortran git curl wget unzip bzip2 \
  libeigen3-dev libboost-all-dev libjsoncpp-dev libopengl-dev mesa-utils \
  libglfw3-dev libglm-dev libopencv-dev libglew-dev libgl1-mesa-dev \
  libssl-dev libjpeg-dev libfreetype6-dev \
  ffmpeg htop iperf3 graphviz
```

- **`libssl-dev` 必须装**：上游 README 漏了它，ORB-SLAM3 需要 `openssl/md5.h`。
- 装完确认：`ffmpeg -version | head -1`（期望 `4.4.2-0ubuntu0.22.04.1`）。
- ⚠️ `libopencv-dev`（apt 版）只是为了让 CMake 找得到依赖；**真正链接的是 Phase B 装到 `/usr/local` 的 4.10.0**。

### Phase B — OpenCV 4.10.0 + CUDA（**新 ABI，不加任何 ABI 标志**）

```bash
cd /root
git clone --depth 1 --branch 4.10.0 https://github.com/opencv/opencv.git
git clone --depth 1 --branch 4.10.0 https://github.com/opencv/opencv_contrib.git

cd /root/opencv && mkdir -p build && cd build
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

> ⚠️ **两个必须**：
> 1. `-DWITH_IPP=OFF` —— 否则 IPPICV 从 `raw.githubusercontent.com` 下载会被墙，CMake 直接失败。
> 2. **不要**加 `-DCMAKE_CXX_FLAGS="-D_GLIBCXX_USE_CXX11_ABI=0"` —— 那是旧 ABI 死路（见 §0.2 约束 2）。

**验收**：
```bash
ls /usr/local/lib/cmake/opencv4/OpenCVConfig.cmake   # 必须存在
ldconfig -p | grep -c opencv                         # 期望 ~138
ls /usr/local/include/opencv4/opencv2/{cudawarping,cudaimgproc,cudastereo,cudaarithm,cudafilters}.hpp
```

### Phase C — LibTorch 2.1.2（**必须是 cxx11-abi 变体**）

```bash
cd /root
curl -L -C - -o libtorch-2.1.2-cxx11-abi.zip \
  'https://download.pytorch.org/libtorch/cu121/libtorch-cxx11-abi-shared-with-deps-2.1.2%2Bcu121.zip'
unzip -q -o libtorch-2.1.2-cxx11-abi.zip     # → /root/libtorch
```

> 下载 ~2.33 GB。国内直连慢：可本地代理下载后 `scp` 上传，或断点续传（`-C -`）。
> ⚠️ 文件名里的 **`cxx11-abi`** 是关键；默认版 `libtorch-shared-with-deps` 是**旧 ABI**，用了会重蹈 §0.2 约束 2 的覆辙。

**验收**：
```bash
ls /root/libtorch/share/cmake/Torch/TorchConfig.cmake   # 必须存在
```

### Phase D — Photo-SLAM：clone + 打补丁 + 编译

```bash
cd /root
git clone https://github.com/HuajianUP/Photo-SLAM.git
cd /root/Photo-SLAM
git checkout f8bfb2f                     # ← 上游基线（本仓库补丁就是基于它）
git apply /root/viewfinder-repo/cloud/patches/photoslam_source_patches.patch
```

> **如果上游仓库不可达 / 已变更**：用 `cloud/patches/modified_files/` 里的完整文件副本直接覆盖同名路径，
> 并单独把两个相机配置放进 `cfg/ORB_SLAM3/Monocular/TUM/`。这条兜底路径不依赖上游，务必保留。
>
> 💡 **网络受限时的镜像**：当前实例实际用的是 `https://githubproxy.cc/https://github.com/HuajianUP/Photo-SLAM.git`。
> 补充 `git remote add upstream https://github.com/HuajianUP/Photo-SLAM.git` 可直接与上游比对。
> ⚠️ 打补丁前务必先做 §0.3 的 `git apply --check`：补丁的上下文行依赖 `f8bfb2f` 这个精确基线。

补丁做了 7 件事（对照 `cloud/patches/modified_files/`）：

| 文件 | 改动 | 里程碑 |
|---|---|---|
| `CMakeLists.txt` | `CUDA_ARCHITECTURES "75;86"` → `"89"` | M0 |
| `build.sh` | 重写：`set -e`、`OpenCV_DIR=/usr/local/lib/cmake/opencv4`、`Torch_DIR=/root/libtorch/...`、`-j4` | M2 |
| `examples/tum_mono.cpp` | 一次性 `LoadImages` → **轮询目录增量喂帧** + `DONE` 哨兵；M7.5 丢跟踪状态日志 + 重初始化卡住兜底 + `TUM_PACE_MS` 回放节流钩子 | M4.1 / M7.5 |
| `include/gaussian_model.h` | 新增 `gaussian_id_` / `next_gaussian_id_` / `last_exported_max_id_` + `saveSplatDelta()` / `saveSplatFull()` 声明 | M4.3 / M7.1 |
| `src/gaussian_model.cpp` | 稳定高斯 ID 的创建/追加/裁剪；`saveSplatDelta()` 写 `deltas/delta_<iter>.bin`（64 位 id + 62×f32）；`saveSplatFull()` 写 `deltas/full_<iter>.bin`；两者均**原子改名**（`.tmp` → 正式名） | M4.3 / M7.1 |
| `src/gaussian_mapper.cpp` | 去掉 `SLAM_ended_` 短路（让训练跑到 `max_num_iterations`）；新关键帧后触发 delta 导出；最终结果同时写 `live/snapshot.ply` + `saveSplatFull()` | M3.7 / M4.2 / M5.2 / M7.1 |
| `cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml` | `max_num_iterations: 15000`、`opacity_reset_interval: 3000`、`prune_big_point_after_iter: 15000`、`densify_until_iter: 15000` | M4.0 / M5.3 |

> **补丁里还有一段无害的死代码**：`examples/tum_mono.cpp` 第 117–118 行的
> `lostSinceT` / `kLostResetAfterS = 15.0`（「丢跟踪 15s 重置」）**已声明但从未使用**。
> 那是被否决的旧方案残留（ORB-SLAM3 自身已含丢跟踪自动重初始化，应用层再做是死代码），
> 真正生效的是状态日志 + `NOT_INITIALIZED>30s → ResetActiveMap()`。
> 只产生一个编译警告，**不影响功能**；`modified_files/` 忠实保留了云端真实现状。
> 详见 `cloud/README.md`。

**编译**（约 15–25 min）：

```bash
cd /root/Photo-SLAM
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
bash build.sh                      # 期望结尾 BUILD_DONE
```

**验收**：
```bash
ls /root/Photo-SLAM/bin/    # 期望 7 个：tum_mono tum_rgbd euroc_stereo replica_mono replica_rgbd train_colmap view_result
ls /root/Photo-SLAM/lib/    # 期望 5 个：libcuda_rasterizer libgaussian_mapper libgaussian_viewer libimgui libsimple_knn
ldd /root/Photo-SLAM/bin/tum_mono | grep -E "opencv|torch" # 应指向 /usr/local 与 /root/libtorch，无 not found
```

### Phase E — conda `myconda` + MonoGS（**M6 渲染流水线的硬依赖**）

> ⚠️ **为什么需要**：`m6/render_pose.py` 与 `m6/prune_f4.py` 里 `sys.path.insert(0, "/root/MonoGS")`，
> 然后 `from gaussian_splatting.gaussian_renderer import render` ——
> **取景推荐的候选渲染直接复用 MonoGS 的 CUDA 光栅器**。这不是「可选冒烟测试」，是运行时依赖。
> `m6/auto_dehaze.sh` 与 `m6/framing_for_case.sh` 也写死了 `/root/miniconda3/envs/myconda/bin/python`。

**注意**：MonoGS 自带的 `environment.yml` **不能直接用**（它 pin 的是 python 3.7.13 + torch 1.12.1 + cudatoolkit 11.6），
**与实例上实际的 `myconda`（python 3.12.4 + torch 2.6.0）完全不符**。
权威规格是提取出来的 `cloud/env/myconda.requirements.txt`。

```bash
# 1) 装 miniconda3 到 /root/miniconda3（base）
#    （用官方安装器，安装路径必须 /root/miniconda3）

# 2) 建环境（名字与路径不可变）
conda create -y -n myconda python=3.12
source /root/miniconda3/etc/profile.d/conda.sh && conda activate myconda

# 3) 先装 torch 2.6.0 + torchvision 0.21.0（CUDA 轮子）
pip install torch==2.6.0 torchvision==0.21.0

# 4) 再按其快照补齐其余依赖
pip install -r /root/viewfinder-repo/cloud/env/myconda.requirements.txt
#    若快照里 -e 形式的 MonoGS 子模块项报错，跳过它们，交给第 6 步

# 5) 克隆 MonoGS（基线 6c9254c）
cd /root && git clone https://github.com/muskie82/MonoGS.git
cd /root/MonoGS && git checkout 6c9254c
git submodule update --init --recursive
#    diff-gaussian-rasterization → rmurai0610/diff-gaussian-rasterization-w-pose @43e21bf
#    simple-knn                  → gitlab.inria.fr/bkerbl/simple-knn @44f7642

# 6) 编译两个 CUDA 扩展（必须指定 sm_89）
export CUDA_HOME=/usr/local/cuda-12.1
export PATH=/usr/local/cuda-12.1/bin:$PATH
export TORCH_CUDA_ARCH_LIST="8.9"
pip install -e submodules/diff-gaussian-rasterization
pip install -e submodules/simple-knn
```

**关键包清单**（重建后逐一核对，版本以 `cloud/env/myconda.requirements.txt` 为准）：

| 包 | 实测版本 | 用途 |
|---|---|---|
| python | 3.12.4 | — |
| torch | 2.6.0 | MonoGS 渲染、`m6` 脚本 |
| torchvision | 0.21.0 | — |
| numpy | 2.2.6 | 全体 m6 脚本 |
| open3d | 0.20.0 | 点云/PLY 处理 |
| plyfile | 1.1.5 | `m6` 里 13 个脚本依赖 |
| scipy | 1.14.0 | `candidate_gen.py` / `l1_score.py` |
| pillow | 10.3.0 | 图像 |
| ImageIO | 2.37.4 | 图像 |
| matplotlib | 3.9.0 | 统计图 |
| pyiqa | 0.1.16 | `l1_pyiqa.py`（L1 画质打分） |
| requests | 2.34.2 | VLM 调用 |
| trimesh | 5.1.0 | 网格 |

**验收**：
```bash
/root/miniconda3/envs/myconda/bin/python -c "
import torch, numpy, plyfile, scipy, PIL, open3d, pyiqa
print('torch', torch.__version__, 'cuda', torch.cuda.is_available())
print('numpy', numpy.__version__, 'open3d', open3d.__version__)
"
/root/miniconda3/envs/myconda/bin/python -c "
import sys; sys.path.insert(0, '/root/MonoGS')
from gaussian_splatting.gaussian_renderer import render; print('MonoGS render OK')
"
```

> ⚠️ **别忘了 conda `base` 也要有 numpy**：`m6/run_local_video.sh` 用的是 `PYTHON="${PYTHON:-/root/miniconda3/bin/python}"`（即 **base**，实测 Python 3.11.4 + numpy 2.0.0）。
> 校验：`/root/miniconda3/bin/python -c "import numpy; print(numpy.__version__)"`

### Phase F — Node 18 + `ws`（下行 WS/HTTP 服务）

实测：node **v18.17.1**（解包在 `/usr/lib/node-v18.17.1-linux-x64`，软链 `/usr/local/bin/node`），npm 9.6.7。

```bash
# 方式一：官方预编译包解到 /usr/lib + 软链（与当前实例一致）
cd /usr/lib && tar -xf node-v18.17.1-linux-x64.tar.xz
ln -sf /usr/lib/node-v18.17.1-linux-x64/bin/node /usr/local/bin/node
ln -sf /usr/lib/node-v18.17.1-linux-x64/bin/npm  /usr/local/bin/npm

# 方式二：nvm / NodeSource 均可，只要 node -v 是 18.x
node -v && npm -v
```

`ws` 依赖**不入库**，由 `cloud/setup/` 里的安装脚本装（注意用官方 registry）：

```bash
mkdir -p /root/m4_ws && cd /root/m4_ws
cat > package.json <<'EOF'
{ "dependencies": { "ws": "^8.21.3" } }
EOF
npm install --registry https://registry.npmjs.org      # 实测装出 ws 8.21.3
```

### Phase G — 部署运行期脚本、配置与服务

把仓库同步到实例（**仓库是唯一真源**）：

```bash
# 云上放一份仓库（用于取补丁/快照；也可只手动 scp 需要的文件）
# 推荐：git clone 到 /root/viewfinder-repo

# Mac 侧执行（读 .env，推送 scripts/ + m6/ + photo_slam_config/）
cd /path/to/ViewFinder
bash scripts/vf_deploy.sh
```

`vf_deploy.sh` 会推送：

| 本地源 | 云端目标 |
|---|---|
| `scripts/m3_pull_rtsp.sh` | `/root/m3_pull_rtsp.sh` |
| `scripts/m4_start.sh` / `m4_stop.sh` | `/root/m4_start.sh` / `/root/m4_stop.sh` |
| `scripts/m4_ws_server.js` | `/root/m4_ws/server.js` |
| `scripts/m7_ws_delta.js` | `/root/m4_ws/m7_ws_delta.js` |
| `m6/*.sh` + `m6/*.py` | `/root/m6/` |
| `photo_slam_config/tum_mono.yaml` | `/root/Photo-SLAM/cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml` |
| `photo_slam_config/phone_1280x720.yaml` | `/root/Photo-SLAM/cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml` |

> ✅ 已核对：上述文件的**云端哈希与本地源完全一致**，`vf_deploy.sh` 是可复现的同步机制。
> ⚠️ `server.js` 改动**必须重启 node** 才生效（`vf_up.sh` 会重启）。

再补齐不入库的辅助文件（可选，历史参考）：

```bash
# cloud/upgrade/*.py → /root/  （与部署无关，仅在需要重新打补丁时用）
# cloud/legacy/m4_ws_server.py → /root/
```

创建运行时目录：

```bash
mkdir -p /root/Photo-SLAM/results/m5_live/live   # WS 服务的默认快照路径
mkdir -p /root/live                              # 拉流落盘目录
```

### Phase H — 密钥与配置

**云上** `/root/m6/.env`（**不随 `vf_deploy.sh` 同步，必须手工写**）：

```bash
cat > /root/m6/.env <<'EOF'
DEEPSEEK_KEY=<你的 DeepSeek API Key>
EOF
chmod 600 /root/m6/.env
```

**Mac 上** `.env`（模板见 `.env.example`）：

```bash
cd /path/to/ViewFinder
cp .env.example .env && chmod 600 .env
# 填入：viewfinder_host / viewfinder_port / viewfinder_username / viewfinder_password
#       ip_webcam_lan_ip / deepseek key
```

### Phase I — 起链路并端到端验收

```bash
# Mac 侧一键拉起（反隧道 + 正隧道 + 云 node 服务 + 健康检查）
bash scripts/vf_up.sh
bash scripts/vf_status.sh
```

期望看到：`云→手机(18080) HTTP 200`、`Mac:8765 OK`、`云 node: 运行中`、cases 列表有返回。

---

## 4. Mac 侧重建

### 4.1 基础

| 需求 | 版本 | 说明 |
|---|---|---|
| macOS | 26.x（arm64） | 实测环境 |
| Node.js | ≥ 18 | `serve-splat-web.mjs` 是**零依赖** ESM 服务 |
| Python 3 | 3.x | `scripts/android_intrinsics.py`、`calib_chessboard.py` |
| adb | platform-tools | 读手机相机内参（`scripts/adb.sh`） |

```bash
brew install node python3
# Android platform-tools（adb）
#   下载 https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
#   解压到 tools/platform-tools/
```

### 4.2 本地 3DGS 查看器（必需）

```bash
cd /path/to/ViewFinder
chmod +x tools/open-web-viewer.command
./tools/open-web-viewer.command          # → http://127.0.0.1:8099/
```

- 服务：`tools/serve-splat-web.mjs` —— **零依赖** Node 静态服务，只监听 `127.0.0.1:8099`。
- 页面：`tools/splat-web/dist/` —— **纯静态、手写源码**（`app.js` + `index.html`），用 import map 直接引
  `dist/vendor/three/` 与 `dist/vendor/gs3d/`，**没有打包器、没有构建步骤**（改完刷新即生效）。
- `tools/splat-web/node_modules/`（`@mkkellogg/gaussian-splats-3d` + `three`）**仅开发时需要**：
  ```bash
  cd tools/splat-web && npm install
  ```
- **实时模式**：`http://127.0.0.1:8099/?live=ws://localhost:8765`
- 联调自检：`node tools/verify-web-viewer.mjs`（无头 Chrome 验证 `liveConnected` / `firstRender` / 高斯数 + 截图）。

### 4.3 Brush.app（可选，原生 Metal 查看/训练器）

```bash
# 1) 下载第三方发行版（arm64）
#    实际使用的归档：tools/_downloads/brush-app-aarch64-apple-darwin.tar.xz
#    校验（必须匹配）：
#      65b2631398c839be3c1d4d7160fe2326389dec87830aac0710985e6690a1048c
# 2) 解压到 tools/brush/
# 3) 用 tools/_build/ 里的脚本重新包装成可双击的 .app：
#      Brush.applescript + Info.plist + make_icon.py（→ AppIcon.icns）
```

> 🟡 **建议把 `tools/_build/`（1.4 MB）纳入版本库**：Brush.app 的包装脚本 + 图标是自制的、不可再生。
> `tools/_downloads/`（107 MB）保持排除，只需记录下载 URL 与 sha256。

### 4.4 `.env`

见 §3 Phase H。**注意**：`.env` 里的 `ip_webcam_lan_ip` 是手机当前 LAN IP，
手机换网/重连后 IP 会变，改完 `.env` 后重跑 `bash scripts/vf_up.sh` 即可重启反隧道。

---

## 5. 验收清单（逐项可执行）

### 5.1 云环境（Phase A–F 后）

| # | 检查 | 期望 |
|---|---|---|
| 1 | `nvidia-smi --query-gpu=name,driver_version --format=csv,noheader` | `NVIDIA GeForce RTX 4090, <驱动>` |
| 2 | `nvcc --version \| tail -2` | `release 12.1` |
| 3 | `ls /usr/local/lib/cmake/opencv4/OpenCVConfig.cmake` | 存在 |
| 4 | `ldconfig -p \| grep -c opencv` | ~138 |
| 5 | `ls /root/libtorch/share/cmake/Torch/TorchConfig.cmake` | 存在 |
| 6 | `ls /root/Photo-SLAM/bin/` | 7 个可执行文件 |
| 7 | `ldd /root/Photo-SLAM/bin/tum_mono \| grep "not found"` | **无输出** |
| 8 | `node -v` | `v18.x` |
| 9 | `ls /root/m4_ws/node_modules/ws` | 存在（8.21.3） |
| 10 | `ffmpeg -version \| head -1` | 4.4.2 |
| 11 | myconda 导入自检（§3 Phase E） | `torch 2.6.0 cuda True` + `MonoGS render OK` |

### 5.2 补丁正确性（**最关键的一项**）

```bash
# 在云上：改动清单必须与仓库记录的完全一致（7 个 M + 3 个 ??）
cd /root/Photo-SLAM
git status --short
# 期望（顺序可能不同）：
#  M CMakeLists.txt
#  M build.sh
#  M cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml
#  M examples/tum_mono.cpp
#  M include/gaussian_model.h
#  M src/gaussian_mapper.cpp
#  M src/gaussian_model.cpp
# ?? cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml
# ?? cfg/ORB_SLAM3/Monocular/TUM/phone_720p.yaml
# ?? run_photoslam.sh

# 且 M7.1 / M7.5 特征必须在
grep -c saveSplatFull src/gaussian_model.cpp      # ≥ 1
grep -c "M7.5" examples/tum_mono.cpp              # ≥ 2
grep -c "rename(delta_tmp\|rename(full_tmp" src/gaussian_model.cpp   # = 2（原子改名）
grep -E "max_num_iterations|opacity_reset_interval" \
  cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml # 期望 15000 / 3000

# 已知无害残留：这段「丢跟踪 15s 重置」是被否决方案，声明了但从未使用（仅编译警告）
grep -n "kLostResetAfterS" examples/tum_mono.cpp  # 只在第 118 行出现（声明处），无使用处

# m6/abl 必须存在（vf_deploy.sh 不同步，靠手工提取）
ls /root/m6/abl/tum_mono_tuned.yaml /root/m6/abl/cfg/{tum_mono,nopyr}.yaml
```

### 5.3 全链路（Phase I 后）

| # | 步骤 | 期望 |
|---|---|---|
| 1 | 手机开 IP Webcam（横屏 1280×720） | RTSP 可用 |
| 2 | `bash scripts/vf_up.sh` | 三项健康检查全绿 |
| 3 | 浏览器开 `http://127.0.0.1:8099/?live=ws://localhost:8765` | 页面加载、WS 连上 |
| 4 | 点「▶ 开始推流」→ 静止 2–3s → 缓慢扫动 40–60s | 云端起 ffmpeg + tum_mono |
| 5 | 观察浏览器 | ~20–25s 出首个快照（需攒 20 关键帧），之后 ~5s 整幅刷新 |
| 6 | 点「⏹ 停止推流」 | 停流 + `DONE` 哨兵 → 后台精修到 15000 迭代 |
| 7 | 状态流 | 已完成建模 → 除雾中 → 除雾完成 |
| 8 | 「云 case」下拉选中该 case | 可切换 原始/除雾 模型 |
| 9 | 点「🖼 帮我构图」 | 已有则直出 Top-3；否则后台 10–15 min 后 `framing_done` 自动刷新 |

**结果目录结构**（`vf_case.sh list` 可查）：

```
/root/Photo-SLAM/results/cases/<YYYYMMDD_HHMMSS>/
  original/point_cloud.ply        # 原始模型
  original/cameras.json
  original/psnr.txt / input.ply
  dehazed/dehazed.ply             # 除雾模型（自动跑）
  framing/{framing.json, *.png}   # 取景推荐 Top-3（按需生成）
  manifest.json                   # {original:done, dehaze:done}
```

### 5.4 回归基线（可选但推荐）

`scripts/vf_local.sh` 用**本地干净视频**隔离「流式链路」与「拍摄/内参」问题：

```bash
bash scripts/vf_local.sh ./sample_videos/m6_test_video.mp4 10
```

打印训练视角数、覆盖源帧范围、位置范围、轨迹总长、光轴最大转角。
**判读**：干净视频能重建出完整正/侧/背/俯 → 问题在流式链路；本地也丢 → 拍摄方式或内参问题。

---

## 6. 已知陷阱（**全部为实际踩过并修复的**）

### 6.1 构建 / ABI

| # | 陷阱 | 对策 |
|---|---|---|
| 1 | **C++ ABI 不匹配**：conda torch 是旧 ABI，Ubuntu 22.04 的 OpenCV/jsoncpp/boost 是新 ABI | 用 **cxx11-abi LibTorch 2.1.2** + 系统默认新 ABI，**零 ABI 改造**。`cloud/legacy/rebuild_abi.sh` 是死路留档，**不要跑** |
| 2 | `CUDA_ARCHITECTURES "75;86"` 硬编码 | 改成 `"89"`（补丁已含） |
| 3 | IPPICV 下载被墙 → CMake 失败 | `-DWITH_IPP=OFF` 必须加 |
| 4 | ORB-SLAM3 找不到 `openssl/md5.h` | 装 `libssl-dev`（上游 README 漏了） |
| 5 | `build.sh` 并行竞态 | 用 `-j4`（不是 `-j`） |
| 6 | Photo-SLAM 用 conda torch 编译 | **不需要**；Photo-SLAM 只用独立 LibTorch，conda 仅供 MonoGS/`m6` 用 |
| 7 | 官方 pin 的 torch 1.13+cu116 在 4090 跑不了 | 覆盖为 2.x（本路线用独立 LibTorch 2.1.2 规避） |

### 6.2 运行时 / 路径

| # | 陷阱 | 对策 |
|---|---|---|
| 8 | **硬编码 `/root`**：`m6/render_pose.py` → `/root/MonoGS`；`m6/*.sh` → `/root/miniconda3/envs/myconda/bin/python`、`/root/m6`；`m6/baseline_stats.py` → `/root/live` | 新实例**必须** `/root` 家目录 + `myconda` 同名同路径（见 §0.2 约束 1） |
| 9 | **`pkill -f` 自匹配**：`pkill -f "node server.js"` 会杀掉自己的重启 shell | 用字符类打断：`pkill -f "serve[r].js"`、`pkill -f "tum_mon[o]"`、`pkill -f "ffmpeg -nostdi[n]"`（脚本已统一如此） |
| 10 | **Mac 没有 `setsid`** | 本机后台进程用 `nohup ... & disown`；云上 Linux 才用 `setsid` |
| 11 | **云端 18080 端口被残留隧道占死**：旧隧道 client 死后 sshd 会抱住反向转发端口 ~2 小时 | `vf_up.sh` 已含：`ss -tlnpH "sport = :18080"` 找 pid 并 kill |
| 12 | node 重启**误触发归档** | `server.js` 启动时以当前 snapshot 为基线，重启不触发 finalize（已修） |
| 13 | 假时间戳 | `m3_pull_rtsp.sh` v2 改用**文件真实 mtime** + `-vsync 0` passthrough，不再用 `(序号-1)/fps` 伪造 |
| 14 | Node 读到半截文件 | `saveSplatDelta` / `saveSplatFull` 均写 `.tmp` 再**原子改名** |
| 15 | Node 跨采集读到旧 delta | `m7_ws_delta.js` 含跨采集**迭代号回退检测** |

### 6.3 采集 / 质量

| # | 陷阱 | 对策 |
|---|---|---|
| 16 | **相机静止建不了图** | 必须**先静止 2–3s**（拿干净首帧）再缓慢扫动 40–60s |
| 17 | **原地转身（旋转主导）会丢跟踪** | 单目 ORB-SLAM 对纯旋转天然弱；要**平移 + 环绕**，每 90° 略停留 |
| 18 | 有效帧率只有 ~3fps | 手机 RTSP 经反隧道降速，是「丢跟踪」主因；用 `vf_local.sh` 区分是手机帧率还是隧道限速 |
| 19 | 带宽全挤手机热点一条上行 | 计划换网络设备 / 加独立 AP |
| 20 | 内参粗值导致质量差 | 必须用真值 `fx=892.400 fy=892.253 cx=632.984 cy=357.109`（一加 15，16:9 居中裁剪）。粗值 fx=1280 时 PSNR 19.33 → 真值 25.97 dB |
| 21 | 畸变 k1..k5 未标定（假定 0） | 待棋盘格标定（`scripts/calib_chessboard.py`） |
| 22 | Photo-SLAM 的 PLY **带 `nx,ny,nz` 且恒为 0** | delta 序列化也要带这 3 个 0，浏览器拼出的 PLY 才能与全量快照**逐字节一致**（M7 验收标准） |
| 23 | 丢跟踪后长时间卡住 | M7.5 补丁：`NOT_INITIALIZED` > 30s 且曾 OK 过 → `ResetActiveMap()` |

### 6.4 文档

| # | 陷阱 | 对策 |
|---|---|---|
| 24 | `docs/plan.md` §7 引用的 `docs/gs_slam_deployment.md`、`docs/m2_photoslam_handoff.md`、`docs/proposal_arcore_vio.md` **实际在 `archive/`** | ⬜ 待办：统一到 `docs/archive/` 并修正交叉引用 |
| 25 | `docs/operations_guide.md` 被 `plan.md` / `full_pipeline_guide.md` 引用但**不存在**；真正的内容是 `.tmp/installation_guide.md`（**在 gitignore 目录内**） | ✅ 已修：提升为 `docs/operations_guide.md`（2026-09-28） |
| 26 | **过期补丁 `docs/photoslam_source_patches.patch` 是个陷阱**：`plan.md` / `plan_m7.md` / `operations_guide.md` 都指向它，照做会打出**缺 M5.2/M7.1/M7.5 的旧源码** | ✅ 已修：权威版移到 `cloud/patches/`，过期版移入 `cloud/patches/superseded/`，全部引用已更新 |
| 27 | **`cloud/upgrade/*.py` 与云端源码不一致**（m7.1 缺原子改名、m7.5 生成已否决的旧逻辑） | ✅ 已在 `cloud/README.md` 明确标注「勿用于重建」，重建只信 `cloud/patches/` |
| 28 | 非交互式 SSH 的 `PATH` 里**没有 CUDA**，`nvcc` 会 `command not found` | 快照/构建命令里写全路径 `/usr/local/cuda-12.1/bin/nvcc`（§0.3 第 5 步实测踩到） |
| 29 | Mac **连不上 GitHub**（`git clone` 60s 超时） | 补丁校验改在云实例上做（用 `git archive f8bfb2f` 导出纯净树，不碰工作区）；见 §0.3 方式 B |

---

## 7. 版本锁定表

> 📌 **权威副本在 [`VERSIONS.md`](VERSIONS.md)**（含每个版本的核对命令、机器可读快照位置、
> 内参与训练参数锁定值）。下面是为阅读方便的内联摘要——两者若有出入，**以 `VERSIONS.md` 为准**。

> 重建时**优先照抄此表**；偏离版本需自行承担兼容风险。

| 项 | 锁定值 | 备注 |
|---|---|---|
| 系统 | Ubuntu 22.04 (Jammy) | 20.04 亦可 |
| GPU / 驱动 | RTX 4090 / 580.126.20 | 驱动 ≥ 525 |
| CUDA 工具链 | 12.1（nvcc 12.1.105） | `/usr/local/cuda-12.1` |
| OpenCV | **4.10.0**（`+CUDA`, `CUDA_ARCH_BIN=8.9`） | 源码编译到 `/usr/local` |
| LibTorch | **2.1.2+cu121，cxx11-abi** | `/root/libtorch` |
| CUDA 架构 | **sm_89** | `CUDA_ARCHITECTURES "89"` / `TORCH_CUDA_ARCH_LIST=8.9` |
| Photo-SLAM | **`HuajianUP/Photo-SLAM@f8bfb2f`** + 本仓库补丁 | 无子模块（vendored） |
| MonoGS | **`muskie82/MonoGS@6c9254c`** | `render_pose.py` 硬依赖 |
| diff-gaussian-rasterization | `rmurai0610/diff-gaussian-rasterization-w-pose@43e21bf` | MonoGS 子模块 |
| simple-knn | `bkerbl/simple-knn@44f7642` | MonoGS 子模块 |
| conda | `/root/miniconda3` + env **`myconda`** | Python 3.12.4 |
| conda `base` | Python 3.11.4 + numpy 2.0.0 | `m6/run_local_video.sh` 用 base |
| torch / torchvision（myconda） | 2.6.0 / 0.21.0 | — |
| numpy / open3d / plyfile | 2.2.6 / 0.20.0 / 1.1.5 | — |
| scipy / pillow / ImageIO | 1.14.0 / 10.3.0 / 2.37.4 | — |
| matplotlib / pyiqa / requests / trimesh | 3.9.0 / 0.1.16 / 2.34.2 / 5.1.0 | — |
| Node.js / npm | **18.17.1** / 9.6.7 | `/usr/lib/node-v18.17.1-linux-x64` |
| `ws` | **8.21.3** | `/root/m4_ws/node_modules` |
| ffmpeg | 4.4.2-0ubuntu0.22.04.1 | `/usr/bin/ffmpeg` |
| 相机内参（`phone_1280x720.yaml`） | fx=892.400 fy=892.253 cx=632.984 cy=357.109 | 一加 15 出厂标定 + 16:9 居中裁剪，活体 session 实测验证 |
| 训练参数 | `max_num_iterations: 15000`、`opacity_reset_interval: 3000`、`densify_until_iter: 15000`、`prune_big_point_after_iter: 15000`、`sh_degree: 3`（SH=3） | 云端实测值 |
| WS 端口 | 8765（云 node）／8099（Mac 查看器）／18080（云→手机反隧道） | — |
| 快照路径 | `/root/Photo-SLAM/results/m5_live/live/snapshot.ply` | node 启动参数 |

---

## 8. 附录：文件 ↔ 云端路径对照表

> 📖 云实例资产的完整清单（含 sha256、抓取证据、权威性警告）见 **`cloud/README.md`**。
> 下表是重建时的**操作视角**速查。

| 仓库内（真源） | 云端路径 | 同步方式 |
|---|---|---|
| `scripts/m3_pull_rtsp.sh` | `/root/m3_pull_rtsp.sh` | `vf_deploy.sh` |
| `scripts/m4_start.sh` | `/root/m4_start.sh` | `vf_deploy.sh` |
| `scripts/m4_stop.sh` | `/root/m4_stop.sh` | `vf_deploy.sh` |
| `scripts/m4_ws_server.js` | `/root/m4_ws/server.js` | `vf_deploy.sh` |
| `scripts/m7_ws_delta.js` | `/root/m4_ws/m7_ws_delta.js` | `vf_deploy.sh` |
| `m6/*.sh`、`m6/*.py` | `/root/m6/` | `vf_deploy.sh` |
| `photo_slam_config/tum_mono.yaml` | `/root/Photo-SLAM/cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml` | `vf_deploy.sh` |
| `photo_slam_config/phone_1280x720.yaml` | `/root/Photo-SLAM/cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml` | `vf_deploy.sh` |
| `cloud/patches/modified_files/**` | `/root/Photo-SLAM/**`（覆盖） | 手工 `git apply` 或覆盖 |
| `cloud/setup/*.sh` | `/root/*.sh` | 手工 |
| `cloud/upgrade/*.py` | `/root/*.py` | 手工（仅需重打补丁时） |
| `cloud/legacy/m4_ws_server.py` | `/root/m4_ws_server.py` | 手工（历史参考） |
| — | `/root/m6/.env` | **手工写入密钥** |
| — | `/root/Photo-SLAM/bin/tum_mono` | 编译产物（Phase D） |
| — | `/root/MonoGS` | clone（Phase E） |

**云端独有的运行期目录（不入库）**：

| 路径 | 内容 | 体积 |
|---|---|---|
| `/root/Photo-SLAM/results/` | 历史结果 + `cases/` + `m5_live/live/` | 47 GB |
| `/root/live/`、`/root/live_inc/`、`/root/live_trim/`、`/root/live_local_*/` | 抽帧落盘工作目录 | 5.3 GB+ |
| `/root/m6/` | 脚本 + 中间产物（`candidates2.json`、`*.ply`、`video_full*.mp4`） | 625 MB |
| `/root/opencv`、`/root/opencv_contrib`、`/root/jsoncpp` | 源码树（构建用） | 693 MB+ |
| `/root/miniconda3` | conda + `myconda` | 9.2 GB |
| `/root/libtorch` | LibTorch 2.1.2 | 3.9 GB |

---

## 9. 重建后的首次提交检查（Checklist）

> 图例：✅ 已完成（2026-09-28 提取） · ⬜ 待办（属「打包提交」阶段，尚未执行）

### 9.1 云端资产提取（§0.3）—— ✅ 全部完成

- [x] `cloud/patches/photoslam_source_patches.patch` 已从云端**重新生成**（553 行 / 24497 B，sha256 `a014e95a…`）
- [x] `git apply --check` 通过，且打补丁结果与 `modified_files/` **7/7 逐字节一致**
- [x] `cloud/patches/modified_files/` 含 7 个改动文件 + 3 个新增文件（含 2 个相机配置 + `run_photoslam.sh`）
- [x] `cloud/patches/superseded/` 存过期补丁（M4 时代），已移出 `docs/` 的 happy path
- [x] `cloud/patches/pre_m71_backup/` 存 M7.1 前的 3 个源文件
- [x] `cloud/setup/` 含 8 个环境构建脚本；`cloud/legacy/` 含 `rebuild_abi.sh` + `m4_ws_server.py`
- [x] `cloud/upgrade/` 含云版 `m7.1` / `m7.5` / `m7_rebuild_check`，且已在 `cloud/README.md` 标注**勿用于重建**
- [x] `cloud/env/` 含 6 个快照（`myconda.requirements.txt` / `apt-manual.txt` / `dpkg-versions.txt` / `tool-versions.txt` / `monogs-versions.txt` / `photoslam-upstream.txt`）
- [x] `m6/abl/`（3 个 A/B 实验配置）已提取
- [x] `docs/operations_guide.md` 已从 `.tmp/installation_guide.md` 提升（原件仍留在 `.tmp/`）
- [x] `cloud/README.md` 已建（权威文件指引 + 完整清单 + sha256 + 验证证据）
- [x] 全部文档引用已从 `docs/photoslam_source_patches.patch` 改为 `cloud/patches/…`
- [x] 云端校验用临时文件已清理

### 9.2 打包提交 —— ✅ 已完成（2026-09-28，仓库初始提交）

> 注：初始提交里**不写死它自己的 commit hash**（写进去就会自相矛盾）。
> 查当前 hash：`git log --oneline -1`；更新点用 `git log --stat` 看。

- [x] `.env.example` 已建（逐项对照脚本实际读取的 key 名写清）；`.env` 经 `git check-ignore` 确认未被跟踪
- [x] `.gitignore` 已重写：排除 `tools/{_smoke,tools,Brush.app,brush,platform-tools,_venv,_downloads,_captures}/`、`tools/splat-web/{node_modules,.npm-cache,*.log}`、`3DGS_demo/{.venv,.mplcache}`、`**/__pycache__`、`*.log`、`.DS_Store`、`results/`、`sample_videos/*`（保留 `m6_test_video.mp4`）
- [x] `tools/_build/`（Brush 包装脚本 + 图标，不可再生）已纳入
- [x] `tools/splat-web/dist/splats/default.ply`（4.4 MB）已纳入；两个 25 MB 级 ply 已排除（原因与恢复方式写在 `.gitignore` 注释里）
- [x] `VERSIONS.md` 已入库（§7 的权威版）
- [x] `README.md` 已建，首段指向 `build_guide.md`；全部相对链接已校验无死链
- [x] **密钥扫描**：逐值搜索 `.env` 中每一项 → 发现并修复 `archive/m2_photoslam_handoff.md` 泄漏的云 SSH 密码（2 处）→ 复扫全清
- [x] `docs/operations_guide.md` 中硬编码的旧实例 host 改为占位符（重建指南不该钉死某个实例）
- [x] `git init` + 首次提交：**165 文件 / ~30 MB**（`.git` 25 MB）；确认无 `.env`、`results/`、`sample_videos/` 大件、`.tmp/`
- [x] **全新临时目录 `git clone` 验收通过**：
  - 165 文件齐全，`.env` 未泄漏（仅有 `.env.example`）
  - 用**克隆出来的**补丁在云上重做验证 → `git apply --check` 通过 + 7/7 逐字节一致
  - 本地查看器**零 `npm install`** 直接可跑（`dist/vendor/` 已随库）：`/`、`/app.js`、`vendor/three`、`vendor/gs3d`、`splats/default.ply`、`framing/framing.json` 全部 HTTP 200

### 9.3 已知遗留（不阻塞重建）

- [ ] `docs/plan.md` §7 仍引用 `docs/{gs_slam_deployment,m2_photoslam_handoff,proposal_arcore_vio}.md`，实际在 `archive/`（§6.4 第 24 项）
- [ ] `examples/tum_mono.cpp` 里 `lostSinceT` / `kLostResetAfterS` 死代码（无害，仅编译警告）
- [ ] 密钥 `.env` / `/root/m6/.env` 需另行保管（不入库）

---

*最后更新：2026-09-28 · 基于一台矩池云 RTX 4090 实例（域名/端口已隐去，见 `.env`）的实测盘点*
