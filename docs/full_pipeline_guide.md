# ViewFinder 全链路使用指南

> 目标：把「手机 RGB 视频 → 云端 Photo-SLAM 增量建图 → 浏览器实时渲染 + case 归档/除雾」整条链路用起来，并把关键操作沉淀成 `scripts/vf_*.sh` 一键脚本。
>
> 相关文档：环境搭建见 `docs/operations_guide.md`；M6 取景/构图见 `docs/plan_m6.md`。

---

## 0. 架构总览

```
手机(IP Webcam, 1280x720, RTSP)
   │  WLAN
   ▼
Mac ──(反向隧道 -R 18080:手机:8080)──► 云实例 18080 ──► m3_pull_rtsp.sh(ffmpeg 抽帧)
   │                                                      │
   │                                                      ▼
   │                                          m4_start.sh ─► tum_mono (Photo-SLAM 增量建图)
   │                                                      │ snapshot.ply
   │                                                      ▼
   │                                          node server.js (:8765, WS+HTTP)
   │                                                      │ 归档 + 除雾(finalize_case.sh)
   ▼                                                      ▼
Mac 8765 ──(正向隧道 -L 8765)──► 浏览器  http://127.0.0.1:8099/?live=ws://localhost:8765
```

- **反隧道**：云上的 ffmpeg 通过 `127.0.0.1:18080` 拉到手机 RTSP。
- **正隧道**：浏览器通过 `localhost:8765` 连到云的 WS 服务（实时快照/控制/状态 + case 拉取）。
- **手机 IP 变化**：改 `.env` 的 `ip_webcam_lan_ip` 后重启反隧道即可。

---

## 1. 前置条件

- `.env` 已填：`viewfinder_host/port/username/password`、`ip_webcam_lan_ip`。
- 云上已装：Photo-SLAM（`~/Photo-SLAM/bin/tum_mono`）、ffmpeg、node+ws、`~/m3_pull_rtsp.sh`、`~/m4_start.sh`、`~/m4_stop.sh`、`~/m4_ws/server.js`、`~/m6/*`。
- 手机已开 **IP Webcam**（横屏 1280x720，RTSP 可用），与 Mac 同 WLAN。

所有脚本都从 `.env` 读配置，首次用前确认 `.env` 正确。

---

## 2. 脚本清单（`scripts/vf_*.sh`）

| 脚本 | 作用 |
|---|---|
| `vf_env.sh` | 公共环境（被其它脚本 source），读 `.env` + SSH 密码认证 |
| `vf_deploy.sh` | 把本地云侧脚本同步到云端 |
| `vf_up.sh` | 一键拉起：反隧道 + 正隧道 + 云 node 服务 + 健康检查 |
| `vf_status.sh` | 全链路体检（隧道/服务/手机/GPU/建图进程） |
| `vf_capture.sh <start\|stop\|status>` | 采集控制（CLI 版，与网页按钮等价） |
| `vf_case.sh <list\|discard <ts>\|archive <dir>>` | case 管理 |
| `vf_local.sh <视频.mp4> [fps]` | 本地视频直跑诊断（上传→重建→位姿覆盖摘要） |

> 云侧脚本的源文件在 `scripts/`（`m3_pull_rtsp.sh`、`m4_start.sh`、`m4_stop.sh`、`m4_ws_server.js`）和 `m6/`（`auto_dehaze.sh`、`finalize_case.sh`、`run_local_video.sh`、`framing_for_case.sh`，以及 M6.6 流水线 `candidate_gen2.py`/`screen.py`/`vlm_score.py`/`coverage.py` 等）。改完用 `vf_deploy.sh` 一键同步全部 `.sh` + `.py`。

---

## 3. 首次 / 改动后：部署

```bash
cd /path/to/ViewFinder
bash scripts/vf_deploy.sh
```

> ⚠️ `server.js` 改动需重启 node 才生效（`vf_up.sh` 会重启，或手动）。`~/m6/.env`（DEEPSEEK_KEY）是密钥，不随 `vf_deploy.sh` 同步，首次需手动写入。

---

## 4. 一键拉起链路

```bash
bash scripts/vf_up.sh
```

它会：
1. 起反隧道（云 `18080` → 手机，自动重连）；
2. 起正隧道（Mac `8765` → 云 `8765`）；
3. 起云 node 服务（已运行则不动）；
4. 打印健康检查（云→手机 HTTP、Mac 8765、node、cases）。

验证通过后，浏览器开：
```
http://127.0.0.1:8099/?live=ws://localhost:8765
```

---

## 5. 实时采集流程（一次完整建图）

1. 手机对准场景，点网页 **「▶ 开始推流」**（或用 `bash scripts/vf_capture.sh start`）。
   - 云端清空旧数据 + 起 ffmpeg + 起 tum_mono。
2. **先静止 2–3 秒**（SLAM 拿干净首帧初始化）。
3. **缓慢扫动 40–60 秒**：横屏、锁 1x、别切镜头；**要边平移边环绕**（纯原地转身会让单目 SLAM 丢跟踪）。
4. 点 **「⏹ 停止推流」**（或 `bash scripts/vf_capture.sh stop`）。
   - 云端停 ffmpeg + 放 DONE 哨兵，tum_mono 进入精修到 10000 迭代。
5. 等状态依次变：**已完成建模 → 除雾中 → 除雾完成**；完成后刷新「云 case」下拉，选中该 case 查看原始/除雾模型。

> 进度随时 `bash scripts/vf_status.sh` 或 `vf_capture.sh status` 查看。

---

## 6. 结果管理（case）

```bash
bash scripts/vf_case.sh list               # 列出 case（时间戳 + original/dehaze 状态）
bash scripts/vf_case.sh discard <时间戳>    # 丢弃某个 case
bash scripts/vf_case.sh archive <云上结果目录>   # 把结果目录归档成 case（跑除雾）
```

case 结构：
```
~/Photo-SLAM/results/cases/<YYYYMMDD_HHMMSS>/
  original/point_cloud.ply     # 原始模型（M0→M5）
  original/cameras.json
  original/psnr.txt / input.ply
  dehazed/dehazed.ply          # 除雾模型（M6.0c）
  manifest.json                # {original:done, dehaze:done}
```

---

## 7. 取景推荐（帮我构图，按 case）

网页「🖼 帮我构图」按钮按**当前选中的云 case** 工作：

1. **该 case 已生成** → 直接显示它的 Top-3（渲染图 + VLM 点评）；
2. **未生成** → 云端后台跑 M6.6 流水线（约 10–15 分钟），网页显示「生成中」；完成后 **WS 推送 `framing_done` → 浏览器自动刷新显示**。

流水线：`~/m6/framing_for_case.sh <ts>` → `candidate_gen2(候选+渲染) → screen(去重/配额) → vlm_score(VLM 选 Top-3)`，结果存 `cases/<ts>/framing/{framing.json, <name>.png}`。

HTTP 接口（node :8765）：
- `GET /framing?case=<ts>` → 200 已有 / 202 生成中 / 404 未生成
- `POST /framing?case=<ts>` → 触发后台生成（幂等：已生成返回 `{status:"done"}`）
- `GET /framing-image?case=<ts>&name=<name>.png` → Top-3 渲染图
- WS 推送：`{type:"status", phase:"framing_done", case:<ts>}` / `framing_error`

CLI 触发：`bash scripts/vf_case.sh framing <时间戳>`。

依赖：云端 `~/m6/.env` 里有 `DEEPSEEK_KEY`；流水线需 `LD_PRELOAD`（脚本内已设）。未选 case（本地默认）时仍显示旧静态演示数据。

---

## 8. 本地视频直跑诊断（排查「跟踪丢失/视角不全」）

当实时结果只覆盖一小段（如只取到正面、缺背面/侧视/俯视）时，用本地干净视频隔离问题：

```bash
bash scripts/vf_local.sh ./sample_videos/m6_test_video.mp4 10
```

它会：上传视频 → 抽帧（1280x720@10fps，真实时间戳）→ 跑 tum_mono → 打印位姿覆盖摘要：
- 训练视角数、覆盖源帧范围、位置范围、轨迹总长、光轴最大转角（>60°=转到侧面，>120°=有背面）。

**判读**：若本地干净视频能重建出完整正/侧/背/俯（视角数多、帧覆盖 99%、轨迹几十米），说明问题在**流式链路**（帧率/卡顿/时间戳）；若本地视频也丢，则是**拍摄方式或内参**问题。

---

## 9. 关键陷阱（务必记住）

1. **`pkill -f` 自匹配**：`pkill -f "node server.js"` 会杀掉自己的重启 shell。用字符类打断：`pkill -f "serve[r].js"`、`pkill -f "tum_mon[o]"`、`pkill -f "ffmpeg -nostdi[n]"`。脚本里已统一这样写。
2. **Mac 没有 `setsid`**：本机后台进程用 `nohup ... & disown`（云上 Linux 才有 setsid）。
3. **假时间戳已修**：`m3_pull_rtsp.sh` v2 不再用 `(序号-1)/fps` 伪造时间戳，改为**文件真实 mtime**，且 `-vsync 0` passthrough 不再 dup 假帧。`m4_start.sh` 已不再传 `FPS=10`。
4. **有效帧率 ~3fps**：手机 RTSP 经反隧道实际约 3fps（非 10fps），是「丢跟踪」主因之一。若本地视频能跑通，下一步应排查是**手机 IP Webcam 帧率太低**还是**反隧道限速**（直播时分别测直连 vs 经隧道帧率）。
5. **旋转主导会丢跟踪**：单目 ORB-SLAM 对「原地转身看背面」天然弱；拍摄时**平移 + 环绕**，每 90° 略停留。
6. **node 服务重启别误触发归档**：`server.js` 已做「启动时以当前 snapshot 为基线」，重启不会误触发 finalize（避免重复 case）。

---

## 10. 常见问题

| 现象 | 对策 |
|---|---|
| 点按钮没反应 | 隧道在重连，等 2–3s；`vf_status.sh` 看隧道 |
| 建图只覆盖前 1/4 | 帧率低/卡顿/旋转太快 → 见 §9.4、§9.5，用 `vf_local.sh` 定位 |
| 手机 IP 变了 | 改 `.env` 的 `ip_webcam_lan_ip`，`bash scripts/vf_up.sh` 重启反隧道 |
| case 重复/缺 | 用 `vf_case.sh list/discard` 清理；重启 node 不再误触发归档 |
| 模型除雾后高斯数没明显减少 | 场景覆盖全、无近相机雾层，属正常 |
| 帮我构图一直显示同一份 | 未选 case 时是静态演示数据；选云 case 后才按 case 拉取/生成 |
