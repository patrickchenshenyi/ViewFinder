# 本地 3DGS 查看器（macOS / Apple Silicon）

为 ViewFinder 项目装的两套本地高斯泼溅查看器，2026-09 在 macOS 26.6.2 (arm64) 上装好并实测通过。

| 方案 | 形态 | 渲染后端 | 用途 |
|---|---|---|---|
| **Brush.app** | 原生 macOS 应用（`~/Applications`） | Metal（wgpu） | 双击/拖放看 ply，还能训练 |
| **浏览器版** | 本地静态站 + Node 服务 | WebGL2（three.js / GaussianSplats3D） | 与 `docs/plan.md` 的展示端路线一致，便于后续改造成动态 splat |

---

## 1. Brush（原生应用）

**安装位置**：`~/Applications/Brush.app`（Brush v0.3.0，arm64，sha256 已校验）
**命令行**：`/opt/homebrew/bin/brush` → 指向 app 内的 `brush_app`

用法：

```bash
# 双击 Brush.app → 弹出文件选择框
# 或把 .ply 拖到 Brush.app 图标上
# 或命令行
brush ~/path/to/point_cloud.ply --with-viewer
```

说明：

- `Brush.app` 是我用 `osacompile` 做的包装（`Contents/Resources/brush_app` 才是真正的渲染器）。
  这样才有"双击/拖放"的 macOS 应用体验；原始发行版只有一个裸命令行二进制。
- Brush 从**文件所在目录**启动，所以在界面里导出（export）会落在 ply 旁边。
- 应用图标是脚本生成的（`_build/make_icon.py`），不再是通用的 AppleScript 水滴图标。
- 已在 `Info.plist` 里声明 `.ply/.splat/.ksplat/.zip` 关联，优先级为 `Alternate` ——
  它会出现在右键"打开方式"里，但**不会**抢走系统默认关联。

## 2. 浏览器版

```bash
./tools/open-web-viewer.command      # 双击也行：拉起服务并打开浏览器
# → http://127.0.0.1:8099/
```

- 服务：`tools/serve-splat-web.mjs`（零依赖 Node 静态服务，只监听 127.0.0.1:8099）
- 页面：`tools/splat-web/dist/`（纯静态，import map 直接引 `three` 与 `gaussian-splats-3d`，无需打包器）
- 默认场景：`tools/splat-web/dist/splats/default.ply`
  （当前是 `results/phone_720p_smoke/1081_shutdown/.../iteration_1081/point_cloud.ply` 的副本，sha256 一致）

操作：左键拖拽旋转 · 滚轮缩放 · 右键平移 · 把 `.ply` 拖进页面即可加载任意文件 ·
**H** 键隐藏控制面板 · ← / → 切换训练视角。

> **完整的参数说明、推荐取值、URL 参数总表与问题排查见 `docs/splat_web_viewer_usage.md`。**
> 下面只保留要点。

**默认显示参数（已按 Photo-SLAM 自身渲染图实测标定）**：高斯尺寸 `1.35x`、屏幕尺寸上限**关闭**、
抗锯齿**关闭**、低不透明度剔除 `1`。三台训练相机上平均 SSIM 0.9527（原始默认 1.0x 时为 0.9177）。

**自动训练视角**：若场景旁存在 `cameras.json`，查看器默认进入第 1 个训练相机并按 `fy` 对齐焦长 ——
这一点很关键：从训练视角（重建体内部）看画面清晰，而从体外自由视角看会被游离高斯的"雾"糊住
（详见使用说明第 4 节）。

主要 URL 参数：

| 参数 | 含义 |
|---|---|
| `?url=splats/xxx.ply` | 加载 `dist/splats/` 下的指定文件 |
| `?cam=splats/cameras.json&kf=0` | 指定训练相机；`?cam=0` 禁用训练视角、改用自动框景 |
| `?sscale=1.35` | 高斯尺寸倍率（实时生效） |
| `?cap=0` | 单个高斯屏幕尺寸上限（像素）；自由视角被糊住时设 128–192 |
| `?aa=1` | 开启 Mip 抗锯齿（默认关） |
| `?dist=1.9` | 自动框景的相机距离（单位是稳健包围球半径） |
| `?up=1` / `?up=-1` | 世界 Y 轴朝上 / 朝下，默认 -1（COLMAP 约定） |
| `?hud=0` / `?frames=60` | 隐藏面板 / 跑满 60 帧后停住，便于无头截图 |
| `?alpha` `?focal` `?kernel` `?smax` | 诊断用，见使用说明 |

想看别的重建结果：直接拖进页面（最省事），或把文件放进 `tools/splat-web/dist/splats/` 再 `?url=`。
服务**只暴露 `dist/` 目录**，项目里的 `.env`、训练输出不会被本地端口发布出去。

### 实现要点

1. **自动取景**：单目 SLAM 的尺度是任意的，写死相机位置必然看空。页面会先解析文件头算出包围盒再摆相机。
2. **稳健包围盒**：3DGS 输出几乎总有少量游离高斯飘在场景外。当前这份 ply 全量包围球半径 **7.28**，
   而 1%/99% 分位裁剪后只有 **1.36** —— 差 5.4 倍。用全量包围盒取景时场景只占 32% 画面、71% 像素是暗的。
3. **尺寸旋钮走原生通路**：`splatScale` 用 GS3D 的 `setSplatScale`（实时，不重传几何），
   屏幕上限用 `maxScreenSpaceSplatSize`（单位是**像素**，不是 clip space）。

---

## 3. 验证方式与证据

不是"打开没报错"就算通过，每一步都有可复现的证据：

- **Brush 原生**：启动后定时截屏，与无应用时的桌面基线做像素差分 —— 窗口占屏 78%、
  **88,272 种颜色**、色彩均值 (104, 94, 82)、标准差 ~60。不传文件时同一窗口是暗色空查看器
  （均值 34/35/36），两者的差异证明 ply 确实被渲染，而不是开了个空白窗。
- **浏览器版**：`node tools/verify-web-viewer.mjs` 用 CDP 驱动无头 Chrome，断言
  `window.__viewfinder.ready === true`、无页面异常、并留一张截图。
  实测输出：`gl="WebGL 2.0 (OpenGL ES 3.0 Chromium)"`、`count=17846`（与 ply 头部的高斯数完全一致）。
- **渲染参数标定（最硬的一条证据）**：把相机放到 SLAM 训练位姿、视口对齐后，与 Photo-SLAM
  自身渲染器的输出（`results/.../1081_shutdown/image/*.jpg`，PSNR 28–30dB）逐像素比较。
  该目录经与原视频帧比对确认是**渲染图**（最佳相关仅 0.44，不是原图副本）。
  三台相机平均 SSIM：当前默认 **0.9527** vs 原始默认 0.9177；`cap=320` 反而降到 0.8264。
  同配置渲染**逐字节可重复**，故上述比较可复现。
- 截图与对比图在 `tools/screenshots/`：
  `web-viewer-training-cam.png`（当前默认视角）、`compare_browser_vs_photoslam.png`
  （浏览器 / Photo-SLAM / 差异图三联）、`web-viewer-free-view-cap128.png`（自由视角开上限）、
  `brush-native.png`（原生 Brush）、`framing-contact-sheet.png`（早期取景对比）。

## 4. 为什么不是官方那几个

- **官方 SIBR viewer**（graphdeco-inria）：依赖 CUDA，macOS 上跑不了。
- **SuperSplat 桌面版**：`playcanvas/supersplat` 近年 release **不再附任何二进制**，没有 dmg 可下。
- **gsplat.js（HuggingFace）**：npm 上并没有 `@huggingface/gsplat` 这个包（404），它不是按 npm 包发布的。
  本项目 plan 里写的 gsplat.js 属于这条路线，替代品是 `@mkkellogg/gaussian-splats-3d`（本次采用）或
  `@playcanvas/supersplat-viewer`。三者实际都是 **WebGL2** 渲染，plan 里"WebGPU"的描述与现状不符，
  后续做动态 buffer 时值得据此修正预期。
- **MetalSplatter**：需要完整 Xcode（本机只有 CommandLineTools），先跳过。

## 5. 环境备注

- npm 默认 cache（`~/.npm`）在沙箱下写入被拒，故安装时用了项目内 cache：
  `npm install --cache ./tools/splat-web/.npm-cache ...`。你自己在终端直接 `npm install` 不受影响。
- 无头 Chrome 必须带 `--no-sandbox`（否则其自身沙箱初始化失败），崩溃转储重定向到项目内目录。
- 文件清单：
  - `docs/splat_web_viewer_usage.md`（**浏览器版完整使用说明**）
  - `tools/Brush.app`（构建产物，与 `~/Applications/Brush.app` 同源）
  - `tools/_build/`（AppleScript 源、Info.plist、图标生成脚本）
  - `tools/splat-web/`（`dist/` 是成品，`node_modules/` 只用于取 three 与 gaussian-splats-3d 的构建产物）
  - `tools/serve-splat-web.mjs`、`tools/open-web-viewer.command`、`tools/verify-web-viewer.mjs`
  - `tools/_smoke/`（验证脚本与中间截图，可删）
  - `tools/_downloads/`（原始下载包与校验文件，可删）
