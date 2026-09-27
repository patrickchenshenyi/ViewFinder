# cloud/ — 云实例专属资产

> **这个目录解决什么问题**：ViewFinder 的大部分代码在 Mac 本地就是权威源（`scripts/`、`m6/`、`photo_slam_config/`，
> 由 `scripts/vf_deploy.sh` 同步上云）。但**有一批东西只存在于云实例上**——尤其是 Photo-SLAM 的 C++ 源码改动。
> 本目录把那些「只活在实例上」的资产固化进版本库，使得**在没有当前云实例镜像的情况下也能从零重建**。
>
> 重建步骤见根目录 `build_guide.md`。

提取时间：**2026-09-28**，来源实例：矩池云 RTX 4090 实例（Ubuntu 22.04；域名/端口/主机名已隐去，见 `.env`）。
总计约 **488 KB**。

---

## ⚠️ 最重要的一条：重建时用哪个文件

| 用途 | 用这个 | **不要**用 |
|---|---|---|
| 复现 Photo-SLAM 源码改动 | `patches/photoslam_source_patches.patch`（打补丁）<br>或 `patches/modified_files/`（直接覆盖） | ❌ `upgrade/*.py` |
| 复现云环境 | `setup/*.sh` + `env/*` | ❌ `legacy/rebuild_abi.sh` |

**`upgrade/*.py` 是历史残留，与云端实际源码不一致，不可作为重建依据。**

实测证据（2026-09-28）：

| 脚本 | 脚本生成的行为 | 云端**实际源码**的行为 | 结论 |
|---|---|---|---|
| `upgrade/m7.1_patch_save_splat_delta.py` | 直接写 `delta_<iter>.bin` | **有**原子改名（`.tmp` → 正式名，防 Node 读半截） | 脚本**落后于**源码 |
| `upgrade/m7.5_patch_tum_mono_reset.py` | 生成 `lostSinceT` / `kLostResetAfterS=15.0` 的「丢跟踪 15s 重置」逻辑 | 同一对变量**已声明但从未使用（死代码）**；真正生效的是「状态变化日志」+「NOT_INITIALIZED>30s 且曾 OK 过 → `ResetActiveMap()`」 | 脚本生成的是**已被否决的旧方案** |

> `upgrade/m7.5` 脚本里那段死代码，恰好**残留**在云端源码第 117–118 行 —— 证明云端是用旧版脚本打的补丁，
> 之后源码被手工演进到新方案，但**从未用新脚本重新打过补丁**（重打需重编 Photo-SLAM，成本高，且功能无影响）。
>
> 👉 正确处理：**只信 `patches/`**。`upgrade/` 保留仅供追溯「当初是怎么改的」，文件头注释记录了设计取舍过程。

---

## 目录结构

```
cloud/
├── README.md                  ← 本文件
├── patches/
│   ├── photoslam_source_patches.patch   ← 【权威】7 个文件的完整 git diff（553 行）
│   ├── modified_files/                  ← 【权威】变更文件完整副本（不依赖上游仓库的兜底路径）
│   └── pre_m71_backup/                  ← M7.1 补丁前的 3 个源文件（历史参考）
├── setup/                     ← 云环境构建脚本（8 个）
├── env/                       ← 环境版本快照（重建的权威规格）
├── legacy/                    ← 已废弃路线留档（勿用）
└── upgrade/                   ← 云版 patch 脚本（**历史残留，勿用于重建**）
```

---

## patches/ — Photo-SLAM 源码改动

**上游基线**：`https://github.com/HuajianUP/Photo-SLAM.git` @ **`f8bfb2f`**（"Update README.md"）
> 实例上实际用的克隆地址是镜像 `https://githubproxy.cc/https://github.com/HuajianUP/Photo-SLAM.git`。
> 该 fork `.gitmodules` 为空，ORB-SLAM3 + Thirdparty（DBoW2/g2o/Sophus）已 vendored，**无需 `submodule update`**。

**改动清单**（7 个已跟踪文件 + 3 个新增文件）：

| 文件 | 改动 | 里程碑 |
|---|---|---|
| `CMakeLists.txt` | `CUDA_ARCHITECTURES "75;86"` → `"89"`（4090 必需） | M0 |
| `build.sh` | 重写：`set -e`、`OpenCV_DIR=/usr/local/lib/cmake/opencv4`、`Torch_DIR=/root/libtorch/share/cmake/Torch`、`-j4` | M2 |
| `examples/tum_mono.cpp` | 一次性 `LoadImages` → **轮询目录增量喂帧** + `DONE` 哨兵；M7.5 状态日志 + 重初始化兜底 + `TUM_PACE_MS` 测试钩子 | M4.1 / M7.5 |
| `include/gaussian_model.h` | `gaussian_id_` / `next_gaussian_id_` / `last_exported_max_id_` + `saveSplatDelta()` / `saveSplatFull()` 声明 | M4.3 / M7.1 |
| `src/gaussian_model.cpp` | 稳定高斯 ID（创建/追加/裁剪同步）；`saveSplatDelta()` 写 `deltas/delta_<iter>.bin`（i64 id + 62×f32）；`saveSplatFull()` 写 `deltas/full_<iter>.bin`；**均原子改名** | M4.3 / M7.1 |
| `src/gaussian_mapper.cpp` | 去掉 `SLAM_ended_` 短路（训练跑到 `max_num_iterations`）；新关键帧触发 delta 导出；最终结果同时写 `live/snapshot.ply` + `saveSplatFull()` | M3.7 / M4.2 / M5.2 / M7.1 |
| `cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml` | `max_num_iterations: 15000`、`opacity_reset_interval: 3000`、`prune_big_point_after_iter: 15000`、`densify_until_iter: 15000` | M4.0 / M5.3 |
| `cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml` ⟵ 新增 | 真机内参 fx=892.400 fy=892.253 cx=632.984 cy=357.109 | M2 |
| `cfg/ORB_SLAM3/Monocular/TUM/phone_720p.yaml` ⟵ 新增 | 竖屏配置（仍是粗值，待确认） | M2 |
| `run_photoslam.sh` ⟵ 新增 | 运行封装 | M2 |

**补丁文件的 sha256**：
```
a014e95ac03873ad1b03a34f28cdc27a9f754ce67dd097cbf8c3b68420ad55c4  photoslam_source_patches.patch
```

### 已验证（2026-09-28，在云实例上执行）

```
1) git archive f8bfb2f | tar -x -C /tmp/ps-pristine        → 473 个文件
2) git apply --check photoslam_source_patches.patch       → ✅ APPLIES CLEANLY
3) git apply photoslam_source_patches.patch
4) 逐文件 cmp 打补丁结果 vs modified_files/               → ✅ 7/7 全部逐字节一致
```

即：**补丁 + 基线 commit 足以精确复现云端源码**；`modified_files/` 是不依赖上游仓库的第二条独立路径。

### 两个同名文件无需额外提取（本地已有同哈希副本）

- `modified_files/run_photoslam.sh` ≡ `scripts/run_photoslam.sh`
- `modified_files/cfg/ORB_SLAM3/Monocular/TUM/phone_720p.yaml` ≡ `photo_slam_config/phone_720p.yaml`
- `modified_files/cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml` ≡ `photo_slam_config/phone_1280x720.yaml`
- `modified_files/cfg/gaussian_mapper/Monocular/TUM/tum_mono.yaml` ≡ `photo_slam_config/tum_mono.yaml`

（`modified_files/` 里仍保留它们，作为校验锚点。）

---

## setup/ — 云环境构建脚本

实例 `/root/` 下的原始脚本，按用途命名保留原名：

| 脚本 | 作用 | 对应 `build_guide.md` |
|---|---|---|
| `apt_deps.sh` | apt 依赖 | Phase A |
| `install_deps_b.sh` / `install_small_b.sh` | 分两批装依赖（拆分是为了绕开个别下载失败） | Phase A |
| `build_opencv_cuda.sh` | 编译 OpenCV 4.10.0 + CUDA 12.1（**新 ABI**） | Phase B |
| `build_photoslam.sh` | 跑 `~/Photo-SLAM/build.sh` | Phase D |
| `compile_b2.sh` / `compile_monogs_b.sh` | 编译 MonoGS 两个 CUDA 扩展 | Phase E |
| `run_slam_b.sh` | MonoGS 冒烟（M1，非最终方案） | 参考 |

> ⚠️ 这些脚本是**当时实际执行的原文**，含硬编码路径与分步试错痕迹；
> `build_guide.md` 里的命令是整理后的**规范版本**，两者应以 `build_guide.md` 为准，本目录用于追溯细节。

## env/ — 环境版本快照（重建的权威规格）

| 文件 | 内容 | 行数 |
|---|---|---|
| `myconda.requirements.txt` | `conda myconda` 的 `pip freeze`（208 条）—— **权威**，因为 MonoGS 自带的 `environment.yml` 与实际环境完全不符 | 208 |
| `apt-manual.txt` | `apt-mark showmanual`（254 条） | 254 |
| `dpkg-versions.txt` | 全量 `dpkg -l`（含版本） | 1460 |
| `tool-versions.txt` | nvcc / node / npm / ffmpeg / nvidia-smi / 三个 Python | 11 |
| `monogs-versions.txt` | MonoGS commit + 两个 submodule commit | 3 |
| `photoslam-upstream.txt` | Photo-SLAM commit + remote | 3 |

**关键版本**（详见 `build_guide.md` §7）：

```
Ubuntu 22.04 · RTX 4090 · driver 580.126.20 · CUDA 12.1 (nvcc 12.1.105)
OpenCV 4.10.0+CUDA @ /usr/local · LibTorch 2.1.2 cxx11-abi @ /root/libtorch
Photo-SLAM @ f8bfb2f · MonoGS @ 6c9254c
node v18.17.1 · npm 9.6.7 · ffmpeg 4.4.2
conda base 3.11.4 · conda myconda 3.12.4 (torch 2.6.0 / numpy 2.2.6 / open3d 0.20.0)
```

> ⚠️ **MonoGS 自带的 `environment.yml` 是错的**（pin 了 python 3.7.13 + torch 1.12.1 + cudatoolkit 11.6），
> 与实际 `myconda`（3.12.4 / 2.6.0）完全不符。**不要用它建环境**，用 `myconda.requirements.txt`。

## legacy/ — 已废弃路线留档（**勿用**）

| 文件 | 为什么废弃 |
|---|---|
| `rebuild_abi.sh` | **旧 ABI 死路**：把 OpenCV/jsoncpp/boost 全重编成 `_GLIBCXX_USE_CXX11_ABI=0`，实测卡在 boost，此路不通。正解是用 cxx11-abi LibTorch + 系统新 ABI（零 ABI 改造）。保留仅为记录踩坑过程。 |
| `m4_ws_server.py` | 早期 Python 版 WebSocket 服务，已被 Node 版 `scripts/m4_ws_server.js` 取代。 |

## upgrade/ — 云版 patch 脚本（**历史残留，勿用于重建**）

见本文开头 §「最重要的一条」。这三个文件是从实例 `/root/` 抓下来的原文，
用于追溯「当初是怎么改源码的」，**不得作为重建依据**（与云端实际源码不一致）。

---

## 未纳入本目录的云端内容（有意排除）

| 路径 | 体积 | 原因 |
|---|---|---|
| `/root/Photo-SLAM/results/` | 47 GB | 训练产物，可再生 |
| `/root/live*`、`live_local_*` | 5.3 GB+ | 抽帧工作目录 |
| `/root/m6/{*.ply,*.mp4,*.json}` | 625 MB | 中间产物（脚本已入库） |
| `/root/miniconda3` | 9.2 GB | 用 `env/myconda.requirements.txt` 重建 |
| `/root/libtorch`、`/root/opencv*` | 4.6 GB | 官方下载 / 源码编译 |
| `/root/m6/.env` | 小 | **密钥**，见 `build_guide.md` §3 Phase H |
| `/root/m6/abl/` | 12 KB | ✅ **已提取，但放在 `m6/abl/`**（属 M6 流水线源码树，不在本目录）——见下 |
| `/root/m6/__pycache__/` | — | 字节码缓存 |

### 关于 `m6/abl/`（容易漏掉的云侧配置）

`vf_deploy.sh` 只遍历 `m6/*.sh` 与 `m6/*.py`，**不会递归子目录**，所以 `m6/abl/` 永远不会被同步到云端 ——
它只在实例上存在过。内容是**训练参数的 A/B 对照配置**：

| 文件 | 与主配置 `photo_slam_config/tum_mono.yaml` 的差异 | 用途 |
|---|---|---|
| `m6/abl/cfg/tum_mono.yaml` | `max_num_iterations: 30000`（主：15000）、`opacity_reset_interval: 0`（主：3000）、`densify_min_opacity: 0.005`（主：0.1）、`densify_until_iter: 25000`（主：15000）、`densify_grad_threshold: 0.0001`（主：0.001） | **调参前的基线**，用于对比 M5.3 的调参收益 |
| `m6/abl/tum_mono_tuned.yaml` | 同 `cfg/tum_mono.yaml` 一系（未调参版本的重命名副本） | 对照组 |
| `m6/abl/cfg/nopyr.yaml` | 在基线基础上关掉金字塔相关项 | 消融实验 |

即：`abl/` 记录了「**调参前后 PSNR/高斯数对比**」所依赖的两套配置。
不含它，就无法复现或质疑 M5.3「15000 是拐点」的结论。
