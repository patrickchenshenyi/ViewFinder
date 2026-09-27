# ViewFinder — M7 增量 3DGS 下行推流（云端 → 浏览器）

> 目标：把「每次整幅 ply 快照重载」改成「高斯增量增删 + 周期全量兜底」，**显著降低浏览器下行带宽**，
> 把省下来的上行带宽让给更高码率的采集（进而提升建模精度）。
> 性质：toy demo，Phase A 优先、Phase B 按需。

---

## 0. 动机

当前瓶颈是**手机热点那一条上行链路**承载全链路流量（M5.3 已定位）。上行方向同时扛着：

1. 手机 → 云端的 RGB 采集流（想要更高码率 → 更准的重建）；
2. 云端 → 浏览器的 3DGS 模型下行（**现在每次 ~20MB / 5s ≈ 32 Mbps**，与上行争抢）。

若把下行从「整幅重载」降到「增量 delta + 稀疏全量」，平均下行可降到 ~5–10 Mbps，省下的带宽直接反哺采集码率。
这正是 M5.1「真·增量增删」的正式落地，独立成 M7 管理。

---

## 1. 现状盘点（已具备的资产）

| 环节 | 现状 | 位置 |
|---|---|---|
| 稳定高斯 ID | `gaussian_id_`（int64 单调递增），`createFromPcd` 初始化、`densificationPostfix` 追加、`prunePoints` 记录删除 + 掩码同步裁剪 | 云 `~/Photo-SLAM/src/gaussian_model.{h,cpp}` |
| delta 导出 | `saveSplatDelta()` 写 `deltas/delta_<iter>.txt`，**目前只有 id**（`iteration`/`total`/`added`/`removed` 各一行 id） | 同上（归档见 `cloud/patches/photoslam_source_patches.patch`） |
| 全量快照 | 每 300 迭代 checkpoint 写 `live/snapshot.ply`，最终结果也覆盖到它 | `gaussian_mapper.cpp` + `savePly` |
| WS 推送 | Node `scripts/m4_ws_server.js`：监控 `snapshot.ply` mtime → 广播**二进制 ply**；连接时回发当前快照（断线恢复）；`start`/`stop` 控制；`final`/`status` 文本广播 | `scripts/m4_ws_server.js`（已上云 `/root/m4_ws/server.js`） |
| 浏览器渲染 | `tools/splat-web/dist/app.js`：`connectLive()` 收二进制 ply → `display()` **整幅重载** `viewer.addSplatScene(blobURL)` | 本地 `dist/`（无构建，刷新即生效） |
| M6 取景推荐 | `/framing` GET/POST、`/framing-image`、`framing_done` 广播（**M7 不得破坏**） | 同上两文件 |

**关键约束**：另一 session 正在维护 M6（取景推荐），M7 的云侧改动（C++ / Node）要等它收尾后再动；
浏览器侧（M7.3）可以先在本地做、不影响云。

---

## 2. 设计总览

### 2.1 Phase A —— delta 增删 + 周期全量 + 浏览器本地累计重建（推荐先做）

```
云端 Photo-SLAM                       Node WS                        浏览器
  saveSplatDelta()                     │                             {id → 62float} 累计表
    新增高斯: id + 完整62float ────────►│─ delta(0x01) ──────────────► 插入表 → 写完整 PLY → display()
    删除高斯: id 列表 ────────────────►│                            从表删除 → 写完整 PLY → display()
  saveSplatFull(周期全量, ~15s) ───────►│─ full(0x00: id+62f32 全量) ► 重建表 → 写完整 PLY → display()
```

- **added**：携带完整参数（xyz + f_dc + opacity + scale + rot + f_rest），浏览器插入累计表。
- **removed**：只带 id，浏览器从表删除。
- **updated（外观精修）**：**不做 delta**，交给周期全量兜底（在线阶段外观变化量小、不划算单独传）。
- **周期全量**：已存在（snapshot 推送），只需**节流到 ~15s** 并作为「权威同步点」，浏览器每次收到全量即重置累计表。
- 收益：稳态精修期 delta 极小，全量 15s 摊薄后平均下行 ~5–10 Mbps（目标从 ~32 Mbps 降）。

### 2.2 Phase B —— 真·原地增删改（GS3D `SplatMesh` GPU buffer）

Phase A 仍在浏览器侧「写 PLY → 整幅重载」（本地重建，但 GPU 上传仍是全量）。
Phase B 直接操作 GaussianSplats3D 的 `SplatMesh` 内部 buffer，按 id 原地增/删/改单个高斯，**彻底不重传不重载**。

- 带宽最低、延迟最短，但侵入 GS3D 内部（需对齐其 buffer 布局 / 排序 / 内存对齐），工程量大、易碎。
- **决策**：先做 Phase A 拿到带宽收益；Phase B 仅在 Phase A 的「本地重载延迟」仍成瓶颈时再做。

---

## 3. 数据格式定义

### 3.1 delta 参数布局（与 `savePly` 完全一致）

单个高斯 = **62 个 float32**（SH=3，与 `GaussianModel::savePly` 输出**逐字段同构**，已实测核对 `snapshot.ply`）：
`xyz(3) + nx,ny,nz(3，恒为 0) + f_dc(3) + f_rest(45) + opacity(1) + scale(3) + rot_quat(4)`。
> 实测：Photo-SLAM 的 PLY **确实带 `nx,ny,nz` 法线字段，且恒为 0**（首 2000 顶点全零），故 delta 序列化也要带这 3 个 0，浏览器拼出的 PLY 才能与全量快照同构。
> f_rest 排布按标准 3DGS `features_rest.transpose(1,2).flatten()` = **通道主序**（`f_rest_[c*15+r] = features_rest[i][r][c]`）；部署时以「重建 PLY 与 `snapshot.ply` 逐字节 diff」做验收。

### 3.2 WS 二进制消息加 1 字节类型标签

现状：浏览器把所有 `ArrayBuffer` 一律当完整 ply。M7 引入二进制信封（双方可控，无向后兼容包袱）：

| 首字节 | 含义 | payload |
|---|---|---|
| `0x00` | **id-indexed 全量同步** | `u32 count` + `count × (i64 id + 62×f32)`（取代裸 ply，见下） |
| `0x01` | delta | 见 3.3 |

> 全量也走「id + 62 float」而非裸 ply，是为了让浏览器**始终以累计表为唯一真相源**：
> `removed` 只能按 id 从表里删，裸 ply 没有 id、删不动。裸 ply（首字节 `'p'`=0x70）仍被浏览器识别为**旧协议**整幅重载，向后兼容。

### 3.3 delta payload（`0x01` 后）

```
u32  added_count
u32  removed_count
for added: i64 id + 62×f32 params      (小端)
for removed: i64 id                    (小端)
```

- fp32 首版；带宽仍高时再降 fp16（62×2=124B/高斯）。
- `0x00` 全量 payload 与 delta 同构：`u32 count` + `count × (i64 id + 62×f32)`（无 removed 段）。
- 文本版（调试用）可复用现有 `delta_<iter>.txt` 的 id 行，另加参数行。

---

## 4. Step-by-step

### M7.1 云端 C++：delta 参数序列化（✅ 已部署并验证）

- [x] 补丁脚本 `scripts/m7.1_patch_save_splat_delta.py`：把 `saveSplatDelta` 从「仅 id 文本」升级为二进制 `delta_<iter>.bin`（added 完整 **62 float** + removed id），并新增 `saveSplatFull` 写 `full_<iter>.bin`（id-indexed 全量）。
- [x] 序列化按 `savePly` 逐字段顺序（`xyz(3)+nx,ny,nz(3,恒0)+f_dc(3)+f_rest(45,通道主序)+opacity(1)+scale(3)+rot(4)`）；`saveSplatFull` 写完重置 `last_exported_max_id_`/`removed_gaussian_ids_` 作全量基线。
- [x] 在 `gaussian_mapper.cpp` **三处**写 `snapshot.ply` 的地方（周期 `%300` + 两处最终结果）追加 `saveSplatFull` 调用。
- [x] 部署：上云跑补丁 → 增量重编 `bin/tum_mono`（`make -j12`，`MAKE_EXIT=0`）。
- **验收（已跑通）**：`m7_rebuild_check.py` 从 `full_10081.bin` 重建 PLY 与 `snapshot.ply` **逐字节一致**（128,800 高斯 / 31,943,931 字节）✓。

### M7.2 云端 Node：WS delta 推送 + 全量节流（✅ 已部署并验证）

- [x] 独立模块 `scripts/m7_ws_delta.js`：`attach({wss, deltasDir})` 轮询 `delta_*.bin`→`0x01`、`full_*.bin`→`0x00`（15s 节流）；新连接先补发最新 `0x00` 全量 + 重放「全量之后」的增量；**迭代号回退检测**（`m4_start.sh` 每次采集会 `rm -rf` 结果目录 → tum_mono 迭代号重来）自动重置基线并回调 `onReset`。
- [x] 集成：`m4_ws_server.js` 移除「连接发裸 snapshot.ply + watch snapshot 广播裸 ply」，改挂 `attach()`；`onPush` 驱动 `pushedOnce`、`onReset` 重置 `pushedOnce`/`finalSent`；`start/stop/final/status/framing` 端点保持不变。
- **验收（已跑通）**：mock 客户端实测 `0x00`(count=2) + `0x01`(added=1/removed=1) 推送正确；跨采集「迭代号回退」测试触发 `onReset` 并正确推新采集数据；集成到生产 `:8765` 的冒烟测试 PASS。

### M7.3 浏览器：累计表 + PLY 写入 + 本地重建（✅ 已完成并验证）

- [x] `tools/splat-web/dist/app.js` 新增 `accumMap = new Map()`（id → 62 float）、`PLY_FIELDS`/`plyHeaderText`/`buildPlyFromAccum`、`parseFullSync`/`parseDelta`、`applyFullSync`/`applyDelta`、`handleBinaryMessage`（首字节 `0x00`/`0x01` 分发，其余当裸 ply 整幅重载，向后兼容）。
- [x] delta 到达后**节流重建**（`ACCUM_REBUILD_MS=500`）。
- [x] 断线重连：服务端连接时补发 `0x00` 全量 → 浏览器重置累计表，天然恢复。
- [x] 暴露 `window.__m7` 供无头验证。
- **验证**：`scripts/m7_selftest.mjs` 全绿（生成头与真实 `m5_original.ply` 头逐字节一致 + full/delta round-trip + 增删重建）；无头 Chrome 实测 `applyFullSync`+`applyDelta` 后累计 3 高斯、`count=3`、`RESULT: PASS`。

### M7.5 跟踪恢复：状态日志 + 重初始化兜底 + ORB 调优（✅ 已部署）

> 背景（2026-09-27 取证）：13:08 采集只有 69 视角/9% 覆盖；13:52 采集 269 视角/77% 但最后 1/5 仍是 0 视角。取证结论：**推流帧率其实正常**（~14fps 平滑、仅 6 次 >0.5s 卡顿）；相机末段仍在运动（相邻帧差 15–20）；且 **ORB-SLAM3 本身已内置丢跟踪恢复**（RECENTLY_LOST 3s 重定位失败 → LOST → 自动建新地图重新初始化，合成黑帧测试验证了状态序列 3→4→0→1）。因此死区大概率是「重初始化卡住 / OK 退化不产新关键帧」，而非网络延迟。

- [x] `scripts/m7.5_patch_tum_mono_reset.py`：`tum_mono.cpp` 加**跟踪状态变化日志**（`[M7.5] 状态变化 …`，真机可诊断死区原因）；兜底「NOT_INITIALIZED>30s 且曾 OK 过」时 `ResetActiveMap` 重试。
- [x] 附带测试钩子：`TUM_PACE_MS>0` 时每帧 usleep（合成序列验证用，实时推流默认关闭）。
- [x] ORB 调优：`iniThFAST 20→10`、`minThFAST 7→5`（低纹理/弱光提取更多特征，提升跟踪与重定位）。
- **验收**：重编通过；合成黑帧测试验证 ORB-SLAM3 内置恢复确实触发（3.1s 后自动新地图）；本地视频冒烟 260 视角无崩溃。真机验收看 `[M7.5]` 日志行。

---

## 5. 验收标准（整体）

| 指标 | 通过标准 |
|---|---|
| 正确性 | 浏览器累计高斯数 == 云端 `total`；delta 反复增删后无重复/无孤儿 id |
| 带宽 | 稳态平均下行从 ~32 Mbps 降到 **~5–10 Mbps**（全量 15s 摊薄后实测） |
| 延迟 | 新关键帧 → delta → 浏览器可见 **< 5s**（目标 1–5s） |
| 断线恢复 | WS 重连后靠 full（`0x00`）重置累计表，无残留/无漂移 |
| M6 回归 | `/framing`、`/framing-image`、`framing_done`、case 切换、除雾状态流全部不变 |

---

## 6. 风险与取舍

| 风险 | 对策 |
|---|---|
| updated（外观精修）delta 缺失导致短期外观滞后 | 靠 ~15s 全量兜底；必要时再补「updated id + 参数」 |
| 浏览器「写 PLY → 全量重载」在大型模型时本地重建变慢 | 节流合并 delta；仍慢则进 Phase B（原地 buffer） |
| 高斯 ID 在 densify/prune 下漂移 | 已实现稳定 ID（M4.3），M7.1 需回归守住 |
| delta 与 full 竞态（重连瞬间） | 服务端保证「先 full 后 delta」的发送顺序 |
| 与 M6 云侧改动冲突 | **M7.1/M7.2 等 M6 session 收尾后再改**；M7.3 本地先行 |

---

## 8. 未来展望（质量改进方向，非本期 M7 范围）

> 背景（2026-09-27）：`video_full.mp4`（182.8s）全链路实测，3DGS 训练视角 PSNR 均值仅 ~20 dB、
> 渲染细节保留 ~13%，根因定位为**单目位姿漂移**（-1.05 dB/100 关键帧）+ 浮尘无淘汰机制（`opacity_reset_interval: 0`）。
> 已落地两项缓解：`opacity_reset_interval: 3000`、F4 视图一致性 prune（`m6/prune_f4.py`，已接入 `auto_dehaze.sh`）。
> 以下为**未落地**的治本方向，留待后续。

### 8.1 单目深度先验（Metric3D / UniDepth）给 3DGS 几何约束 —— 优先级最高

- **动机**：单目 ORB-SLAM3 无绝对尺度、深度不确定，位姿误差随时间累积，直接限制 3DGS 几何质量。
- **方案**：在 Photo-SLAM 增量建图时，用预训练单目深度模型（`Metric3D`、`UniDepth`、或轻量 `Depth Anything V2`）
  对关键帧估计**度量深度**，作为几何先验/监督项注入：
  - 约束高斯沿视线方向的深度分布，抑制「浮尘 + 模糊平均」；
  - 缓解单目尺度漂移，进而稳定相机位姿与致密化。
- **落点**：`gaussian_mapper.cpp` 的深度监督分支（Photo-SLAM 已有 RGB-D/Stereo 深度路径可参照），或后处理几何正则。
- **预期收益**：训练视角 PSNR 从 ~20 dB 抬升，渲染细节保留率显著提升；这是当前质量天花板的主要突破点。

### 8.2 采集规范（拍摄方式 / 时长 / 重叠）

- **减少单次时长**：位姿漂移随时长累积（本次 -1.05 dB/100kf），建议单段控制在 60–90s，必要时分段重建。
- **提高重叠 + 平移主导**：旋转主导易丢跟踪；每 90° 略停留、增加相邻帧重叠。
- 素材来源：后续新素材（如 `video_full_2.mp4`）按此规范拍摄。

### 8.3 其它备选

- 双目 / RGB-D 采集（`docs/gs_slam_deployment.md` 已评估 Photo-SLAM 支持）。
- 深度补全 + 视图一致性更强的 floaters 清理（F4 增强：可见次数阈值自适应）。

---

## 7. 依赖与顺序

1. **M7.3（浏览器）先做**：本地、无云依赖、不与 M6 冲突。
2. **M7.1 → M7.2（云侧）后做**：等 M6 收尾，避免同时改 `m4_ws_server.js` / C++。
3. 联调：先回放一份已有 delta 序列做浏览器单测，再上真机实时链路。
