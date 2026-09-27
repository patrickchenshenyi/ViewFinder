# 全链路操作指南（拉镜像 → 开启实时建图）

> 面向：换新实例 / 重建环境后，把「手机实时流 → 云端增量建图 → 浏览器实时渲染」整条链路重新拉起来。
>
> **日常使用（一键脚本、case 管理、取景推荐）请见 `docs/full_pipeline_guide.md`**；本文件专注「从零重建环境」。

---

## 0. 镜像前提（重要）

**镜像必须包含 M4 的全部改动**（建议现在就在云上重新存一份镜像）：

| 必须包含 | 位置 |
|---|---|
| Photo-SLAM（含 M4 补丁，已编译） | `~/Photo-SLAM/bin/tum_mono` |
| 相机内参（真值） | `~/Photo-SLAM/cfg/ORB_SLAM3/Monocular/TUM/phone_1280x720.yaml` |
| ffmpeg CLI | `/usr/bin/ffmpeg` |
| Node + `ws` 包 | `~/m4_ws/node_modules/ws` |
| 拉流脚本 | `~/m3_pull_rtsp.sh` |
| 起停脚本 | `~/m4_start.sh`、`~/m4_stop.sh` |
| WS 服务端 | `~/m4_ws/server.js` |
| M6 脚本 + 流水线（除雾/归档/本地视频/取景推荐） | `~/m6/*.sh`、`~/m6/*.py` |
| 取景推荐 API key | `~/m6/.env`（`DEEPSEEK_KEY=...`，密钥，重建后需手动写入） |

> ⚠️ 如果镜像还是 **M2 时代的旧镜像**，需先重新应用补丁 `cloud/patches/photoslam_source_patches.patch`（本地工作区有）并重编，再推上 `m3_pull_rtsp.sh` / `m4_*.sh` / `m4_ws/` / `m6/`。
>
> 📖 从零重建整套环境（含云实例与 Mac 侧）：见根目录 **`build_guide.md`**；云实例资产总览见 **`cloud/README.md`**。

---

## 1. SSH 连接（密码认证，脚本友好）

```bash
cat > /tmp/askpass.sh <<'EOF'
#!/bin/sh
printf '%s\n' '<云实例密码>'
EOF
chmod +x /tmp/askpass.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/askpass.sh SSH_ASKPASS_REQUIRE=force
SSHOPT="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -p <云实例端口> root@<云实例域名>"
```

> 💡 **推荐直接复用现成脚本**，它从 `.env` 读配置、无需手填：
> ```bash
> source scripts/vf_env.sh    # 提供 vf_ssh / vf_scp，已封装 SSH_ASKPASS
> vf_ssh 'hostname'
> ```

## 2. 起双向隧道（带自动重连）

反隧道（云 `18080` → 手机 `8080`，供 ffmpeg 拉 RTSP）+ 正隧道（Mac `8765` → 云 `8765`，供浏览器连 WS）：

```bash
while true; do
  ssh -N $SSHOPT -R 18080:<手机LAN-IP>:8080 & RPID=$!
  ssh -N $SSHOPT -L 8765:localhost:8765     & FPID=$!
  wait $RPID $FPID 2>/dev/null; kill $RPID $FPID 2>/dev/null; sleep 2
done
```

> 💡 用 `bash scripts/vf_up.sh` 更省事：它已封装上面两条隧道 + 自动重连 + 云端残留端口清理 + 健康检查。

## 3. 起 WebSocket 服务（云）

服务同时提供 WS（实时快照/控制/状态）+ HTTP（case 列表与模型拉取）：

```bash
ssh $SSHOPT 'cd ~/m4_ws && setsid node server.js ~/Photo-SLAM/results/m5_live/live/snapshot.ply 8765 > /tmp/m5_ws.log 2>&1 < /dev/null &'
```

HTTP 接口（经 `-L 8765` 隧道，浏览器直接访问 `http://localhost:8765`）：
- `GET /cases` → 已完成 case 列表（时间戳 + original/dehaze 状态）
- `GET /ply?case=<ts>&model=orig|dehaze` → 拉对应 ply
- `GET /cameras?case=<ts>` → 拉该 case 的 cameras.json
- `GET /framing?case=<ts>` → 取景推荐（200 已有 / 202 生成中 / 404 未生成）
- `POST /framing?case=<ts>` → 触发取景推荐后台生成（约 10–15 分钟）
- `GET /framing-image?case=<ts>&name=<name>.png` → 取景推荐 Top-3 渲染图

> 源码在本地 `scripts/m4_ws_server.js`（上传到 `~/m4_ws/server.js`）。
> ⚠️ 重启服务时别用 `pkill -f "node server.js"`（会自匹配杀掉重启 shell）；用 `pkill -f "serve[r].js"` 或两步分离。

## 4. 打开浏览器 → 点按钮开始

本地 viewer（先起一次）：
```bash
./tools/open-web-viewer.command   # 起 http://127.0.0.1:8099
```
浏览器开：`http://127.0.0.1:8099/?live=ws://localhost:8765`

操作：
1. 手机先对准场景；
2. 点 **「▶ 开始推流」**（云端清空 + 全新起 ffmpeg+tum_mono，起点=此刻）；
3. **先静止 2–3 秒**（SLAM 拿干净首帧）；
4. **缓慢扫动 40–60 秒**（横屏、锁 1x、别触发切镜头）；
5. 点 **「⏹ 停止推流」**（云端停流 + DONE 哨兵 → 进入后台精修到 10000 迭代）。

> 浏览器约 20–25s 后出第一个快照（要攒 20 个关键帧），之后每 ~5s 整幅刷新。

## 5. 取结果

推流结束（`tum_mono` 退出 = 完整迭代完成）后，服务**自动归档**该 case 并跑除雾：

```
~/Photo-SLAM/results/cases/<YYYYMMDD_HHMMSS>/
  original/point_cloud.ply      # 原始模型（M0→M5）
  original/cameras.json
  original/psnr.txt / input.ply
  dehazed/dehazed.ply           # 除雾模型（M6.0c，自动跑）
  manifest.json                 # {original:done, dehaze:done}
```

- 未完成（被重新开始）的 case 会被 `m4_start.sh` 清空丢弃，**不归档**。
- 浏览器在「停止推流」后会依次提示「已完成建模 → 除雾中 → 除雾完成」；完成后刷新「云 case」下拉即可加载该 case 的原始/除雾模型。

## 6. 常见问题

| 现象 | 原因 / 对策 |
|---|---|
| 点按钮没反应 | SSH 隧道正在重连，等 2–3s 重试 |
| 帧率慢 / 下载慢 | 手机热点上行受限（IP Webcam + Mac SSH + 浏览器下载全挤一条热点），加独立 AP |
| 建图迟迟不初始化 | 相机没动 / 动太慢，ORB-SLAM3 需视差；扫动幅度大一点 |
| 画面糊 / 质量差 | 内参是否真值、码率（IP Webcam 质量档）、场景纹理 |
| 隧道老掉 | 短期用带重连的循环；长期改 frp / autossh |
