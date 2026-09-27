/*
 * Viewfinder 3DGS 本地查看器 — GaussianSplats3D (WebGL2) 前端
 *
 * 关键点：单目 SLAM / 3DGS 训练输出的尺度是任意的，写死相机位置会看空或钻进点云里。
 * 所以这里先解析文件头，算出包围盒与中心，再据此摆放相机。
 */
import * as GaussianSplats3D from '@mkkellogg/gaussian-splats-3d';

// 每个查看器实例用**独立的顶层宿主 div**：GS3D 的 dispose() 是异步的，其收尾逻辑会执行
// document.body.removeChild(rootElement)。若复用同一个容器，异步收尾会把刚建好的新查看器
// 容器一起摘掉（表现为"点重置/切视角后整屏变黑、canvas 0x0"）。独立宿主可彻底避免。
let viewerHost = null;

function newViewerHost() {
  const host = document.createElement('div');
  host.className = 'viewer-host';
  document.body.appendChild(host);
  return host;
}
const metaEl = document.getElementById('meta');
const statusEl = document.getElementById('status');
const dropEl = document.getElementById('drop');
const fileInput = document.getElementById('file');

const DEFAULT_SPLAT = 'splats/default.ply';

let viewer = null;
let currentBuffer = null;
let currentFormat = GaussianSplats3D.SceneFormat.Ply;
let currentName = '';
let upSign = -1; // -1: 世界 Y 轴向下（COLMAP / 多数 3DGS 输出）；+1: Y 轴向上
let firstRenderReported = false;
let liveWs = null; // 实时模式下的 WebSocket 引用（用于发送开始/停止控制）

const PARAMS = new URLSearchParams(location.search);
if (PARAMS.get('up') === '1') upSign = 1;
if (PARAMS.get('up') === '-1') upSign = -1;
let distFactor = Number(PARAMS.get('dist')) > 0 ? Number(PARAMS.get('dist')) : 1.9;

// 显示质量旋钮（也可用 URL 参数预设，见 docs/splat_web_viewer_usage.md）
// splatScale：GS3D 原生的屏幕空间尺寸倍率，等同 Brush 的 "Splat Scale"，可实时调整
//   默认 1.35：以 Photo-SLAM 自身渲染图为标尺、在 2 台训练相机上实测最优
//   （平均 SSIM 0.9503，sscale=1.0 时只有 0.9177）
// maxScreenPx：单个高斯的屏幕尺寸上限（像素）。着色器里是 splatScale * min(extent, cap)。
//   专治少数超大高斯糊满画面，但按参考图实测它会降低保真度（cap=64 时 SSIM 从 0.95 掉到 0.34 相关），
//   故默认关闭。只有在自由视角被游离高斯糊住时才建议打开。
let splatScale = Number(PARAMS.get('sscale')) > 0 ? Number(PARAMS.get('sscale')) : 1.0;
let maxScreenPx = PARAMS.has('cap') ? Math.max(0, Number(PARAMS.get('cap')) || 0) : 0;
let maxScalePct = Number(PARAMS.get('smax')) > 0 ? Number(PARAMS.get('smax')) : 0;
// antialiased：GS3D 的 Mip 滤波会给屏幕空间 2D 协方差加 kernel2DSize 常数，对小高斯偏柔化。
//   在 3 台训练相机上实测：关掉它 SSIM 更高（平均 0.9527 vs 0.9510），锐度也更贴近参考渲染
//   （27.4/28.0/26.2 vs 参考 27.0/28.4/34.5）。故默认关闭，?aa=1 可开启。
let antialiased = PARAMS.get('aa') === '1';
let alphaThreshold = Number(PARAMS.get('alpha')) > 0 ? Number(PARAMS.get('alpha')) : 1;  // 低不透明度剔除阈值
// GS3D 文档：场景很小、高斯很小时增大 focalAdjustment 可改善画质；kernel2DSize 是投影后加到
// 屏幕空间 2D 协方差上的常数（抗锯齿核），默认 0.3
let focalAdjustment = Number(PARAMS.get('focal')) > 0 ? Number(PARAMS.get('focal')) : 1.0;
let kernel2DSize = PARAMS.has('kernel') ? Math.max(0, Number(PARAMS.get('kernel')) || 0) : 0.3;

// 供外部（无头 Chrome / CDP / 自动化脚本）探测的状态
window.__viewfinder = { ready: false, name: '', count: 0, gl: '', error: null, frames: 0 };

/* ---------- 训练相机模式（?cam=<cameras.json>&kf=<id>） ----------
 * 把相机放到 SLAM/3DGS 训练时的真实位姿与内参上。用途：
 *   1) 与重建器自己的渲染图逐像素对比，判断"糊"到底是数据问题还是查看器参数问题；
 *   2) 从训练视角评估重建质量（自由视角容易高估或低估）。
 * cameras.json 约定（Photo-SLAM / 3DGS 输出）：position = 相机中心；rotation = 3x3（相机→世界）；
 * COLMAP 相机坐标系为 +X 右、+Y 下、+Z 前，故视线 = R·(0,0,1)，世界上方 = -R·(0,1,0)。
 */
let trainingCam = null;
let camerasList = null;
let camIndex = 0;

function cameraFromEntry(cam) {
  const R = cam.rotation;
  const p = cam.position.map(Number);
  const fwd = [R[0][2], R[1][2], R[2][2]];
  const up = [-R[0][1], -R[1][1], -R[2][1]];
  return {
    id: cam.id,
    imgName: cam.img_name,
    fy: Number(cam.fy),
    width: Number(cam.width),
    height: Number(cam.height),
    pose: {
      position: p,
      lookAt: [0, 1, 2].map((i) => p[i] + fwd[i]),
      cameraUp: up,
    },
  };
}

async function fetchCameras(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const cams = await res.json();
  if (!Array.isArray(cams) || !cams.length) throw new Error('内容为空');
  return cams;
}

async function loadTrainingCamera(url, kfId) {
  camerasList = await fetchCameras(url);
  let idx = camerasList.findIndex((c) => String(c.id) === String(kfId));
  if (idx < 0) idx = 0;
  camIndex = idx;
  return cameraFromEntry(camerasList[idx]);
}

// 没有显式指定 ?cam= 时，尝试自动找同目录的 cameras.json —— 有它就能默认给出"训练视角"，
// 这比自动框景更接近真实观测（自由视角常常穿过游离高斯的雾，看起来像糊了）。
async function tryAutoCameras(splatUrl) {
  const dir = splatUrl.replace(/[^/]*$/, '');
  for (const cand of [`${dir}cameras.json`, 'splats/cameras.json']) {
    try {
      const cams = await fetchCameras(cand);
      camerasList = cams;
      const idx = cams.findIndex((c) => String(c.id) === String(PARAMS.get('kf')));
      camIndex = idx >= 0 ? idx : 0;
      setStatus(`已载入 ${cams.length} 个训练相机（${cand}）`);
      return cameraFromEntry(cams[camIndex]);
    } catch (_) { /* 试下一个候选 */ }
  }
  return null;
}

function stepCamera(delta) {
  if (!camerasList || !camerasList.length) return;
  camIndex = (camIndex + delta + camerasList.length) % camerasList.length;
  trainingCam = cameraFromEntry(camerasList[camIndex]);
  updateCamBar();
  beacon({ event: 'trainingCam', id: trainingCam.id, img: trainingCam.imgName });
  reRender();
}

function updateCamBar() {
  const bar = document.getElementById('camBar');
  if (!bar) return;
  if (!camerasList || !trainingCam) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  document.getElementById('camLabel').textContent =
    `训练视角 ${camIndex + 1}/${camerasList.length}（id=${trainingCam.id}）`;
}

// 让渲染焦长（像素）等于训练时的 fy：focal = (视口高/2)/tan(fov/2)
function trainingFov() {
  if (!trainingCam) return null;
  const h = Math.max(window.innerHeight, 1);
  return (2 * Math.atan((h / 2) / trainingCam.fy) * 180) / Math.PI;
}

function applyTrainingFov() {
  const fov = trainingFov();
  if (fov === null || !viewer) return;
  const cam = viewer.camera || viewer.perspectiveCamera;
  if (!cam) return;
  cam.fov = fov;
  cam.updateProjectionMatrix();
}

/* ---------- 诊断：把事件回报给本地服务，便于无头验证 ---------- */
function beacon(params) {
  try {
    const q = new URLSearchParams({ t: Date.now().toString(), ...params });
    fetch(`/__beacon?${q}`, { keepalive: true }).catch(() => {});
  } catch (_) { /* 以 file:// 打开时没有服务端，忽略 */ }
  console.log('[viewfinder]', JSON.stringify(params));
}
window.addEventListener('error', (e) => {
  setStatus(`错误：${e.message}`, true);
  beacon({ event: 'error', msg: String(e.message) });
});
window.addEventListener('unhandledrejection', (e) => {
  const msg = e.reason && e.reason.message ? e.reason.message : String(e.reason);
  setStatus(`错误：${msg}`, true);
  beacon({ event: 'error', msg });
});

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle('err', isError);
}

/* ---------- PLY / SPLAT 包围盒解析 ---------- */
const TYPE_SIZE = {
  char: 1, uchar: 1, int8: 1, uint8: 1,
  short: 2, ushort: 2, int16: 2, uint16: 2,
  int: 4, uint: 4, int32: 4, uint32: 4, float: 4, float32: 4,
  double: 8, float64: 8,
};

// 3DGS 输出里几乎总有少量游离高斯（floater）飘在场景外几倍距离处。
// 直接用全量包围盒取景会把相机推得极远、画面里只剩一个小点，
// 所以取景用 1%/99% 分位裁剪后的"稳健包围盒"，同时保留全量跨度用于提示。
const ROBUST_Q = 0.01;

function percentile(sorted, q) {
  const idx = (sorted.length - 1) * q;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
}

function summarize(ax, ay, az) {
  const axes = [ax, ay, az];
  const fullMin = axes.map((a) => a[0]);
  const fullMax = axes.map((a) => a[a.length - 1]);
  const min = axes.map((a) => percentile(a, ROBUST_Q));
  const max = axes.map((a) => percentile(a, 1 - ROBUST_Q));
  const center = [0, 1, 2].map((i) => (min[i] + max[i]) / 2);
  const span = [0, 1, 2].map((i) => max[i] - min[i]);
  const fullSpan = [0, 1, 2].map((i) => fullMax[i] - fullMin[i]);
  return {
    count: ax.length,
    min,
    max,
    center,
    span,
    fullSpan,
    radius: Math.max(Math.hypot(span[0], span[1], span[2]) / 2, 1e-6),
    fullRadius: Math.max(Math.hypot(fullSpan[0], fullSpan[1], fullSpan[2]) / 2, 1e-6),
  };
}

function parsePlyHeader(buffer) {
  const headLen = Math.min(buffer.byteLength, 262144);
  const headText = new TextDecoder('ascii', { fatal: false })
    .decode(new Uint8Array(buffer, 0, headLen));
  const endIdx = headText.indexOf('end_header');
  if (endIdx < 0 || !headText.startsWith('ply')) throw new Error('不是有效的 PLY 文件');
  const headerEnd = headText.indexOf('\n', endIdx);
  if (headerEnd < 0) throw new Error('PLY 头部不完整');

  let count = 0;
  let stride = 0;
  const offsets = {};
  const types = {};
  let inVertex = false;

  for (const rawLine of headText.slice(0, headerEnd).split(/\r?\n/)) {
    const parts = rawLine.trim().split(/\s+/);
    if (parts[0] === 'element') {
      inVertex = parts[1] === 'vertex';
      if (inVertex) count = parseInt(parts[2], 10) || 0;
    } else if (parts[0] === 'property' && inVertex && parts[1] !== 'list') {
      types[parts[2]] = parts[1];
      offsets[parts[2]] = stride;
      stride += TYPE_SIZE[parts[1]] || 4;
    }
  }
  if (!count || !stride) throw new Error('PLY 中没有 vertex 数据');
  return { count, stride, offsets, types, dataStart: headerEnd + 1 };
}

function parsePlyBounds(buffer) {
  const { count, stride, offsets, types, dataStart } = parsePlyHeader(buffer);
  for (const axis of ['x', 'y', 'z']) {
    if (!(axis in offsets)) throw new Error(`PLY 缺少 ${axis} 属性`);
    if (types[axis] !== 'float' && types[axis] !== 'float32') {
      throw new Error(`${axis} 不是 float32，暂时无法自动取景`);
    }
  }

  const available = Math.floor((buffer.byteLength - dataStart) / stride);
  const n = Math.min(count, available);
  if (n <= 0) throw new Error('PLY 数据段为空');

  const dv = new DataView(buffer, dataStart);
  const ox = offsets.x, oy = offsets.y, oz = offsets.z;
  const xs = new Float64Array(n), ys = new Float64Array(n), zs = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const base = i * stride;
    xs[i] = dv.getFloat32(base + ox, true);
    ys[i] = dv.getFloat32(base + oy, true);
    zs[i] = dv.getFloat32(base + oz, true);
  }
  xs.sort(); ys.sort(); zs.sort();
  return summarize(xs, ys, zs);
}

/*
 * 高斯尺寸改写。
 *
 * INRIA 格式的 scale_0..2 存的是 **对数** 尺度（渲染时各自 exp 一次），
 * 所以"把高斯整体放大 k 倍"等价于给对数加上 ln(k)，不用解包再打包。
 * 另外 Photo-SLAM 这类单目输出常有少量尺度巨大的游离高斯（本例 99 分位屏幕直径 127px，
 * 而中位数只有 2px），它们会把画面糊住，故支持按场景尺寸设上限。
 * 只改写 scale_*，位置/颜色/SH/不透明度一律原样保留。
 */
function transformPlyScales(buffer, factor, maxLinear) {
  const { count, stride, offsets, dataStart } = parsePlyHeader(buffer);
  const keys = ['scale_0', 'scale_1', 'scale_2'];
  for (const k of keys) {
    if (!(k in offsets)) {
      return { buffer, changed: false, clamped: 0, total: 0, note: '该 PLY 无 scale_* 属性，尺寸调整已跳过' };
    }
  }

  const out = buffer.slice(0);
  const dv = new DataView(out, dataStart);
  const available = Math.floor((out.byteLength - dataStart) / stride);
  const n = Math.min(count, available);
  const logFactor = Math.log(factor);
  const maxLog = maxLinear > 0 ? Math.log(maxLinear) : Infinity;
  let clamped = 0;

  for (let i = 0; i < n; i++) {
    const base = i * stride;
    for (const k of keys) {
      const o = offsets[k];
      let v = dv.getFloat32(base + o, true) + logFactor;
      if (v > maxLog) { v = maxLog; clamped += 1; }
      dv.setFloat32(base + o, v, true);
    }
  }
  return { buffer: out, changed: true, clamped, total: n * keys.length, note: null };
}

function parseSplatBounds(buffer) {
  const stride = 32;
  const n = Math.floor(buffer.byteLength / stride);
  if (!n) throw new Error('.splat 数据为空');
  const dv = new DataView(buffer);
  const xs = new Float64Array(n), ys = new Float64Array(n), zs = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const base = i * stride;
    xs[i] = dv.getFloat32(base, true);
    ys[i] = dv.getFloat32(base + 4, true);
    zs[i] = dv.getFloat32(base + 8, true);
  }
  xs.sort(); ys.sort(); zs.sort();
  return summarize(xs, ys, zs);
}

function boundsFor(buffer, format) {
  if (format === GaussianSplats3D.SceneFormat.Ply) return parsePlyBounds(buffer);
  if (format === GaussianSplats3D.SceneFormat.Splat) return parseSplatBounds(buffer);
  return null;
}

/* ---------- 相机取景 ---------- */
function normalize3(v) {
  const l = Math.hypot(v[0], v[1], v[2]) || 1;
  return [v[0] / l, v[1] / l, v[2] / l];
}

function framing(bounds) {
  if (!bounds) {
    return { position: [-1, -4, 6], lookAt: [0, 4, 0], cameraUp: [0, upSign, 0] };
  }
  const c = bounds.center;
  const r = bounds.radius;
  const up = [0, upSign, 0];
  const back = normalize3([0.62, 0, 1.0]);
  const d = distFactor * r; // 相机到场景中心的距离（以稳健包围球半径为单位）
  const position = [0, 1, 2].map((a) => c[a] + up[a] * 0.38 * d + back[a] * 0.92 * d);
  return { position, lookAt: c, cameraUp: up };
}

/* ---------- 查看器生命周期 ---------- */
function destroyViewer() {
  if (!viewer) return;
  try { viewer.dispose(); } catch (_) { /* 已释放 */ }
  viewer = null;
}

// 清空当前显示（恢复为空模型）
function clearDisplay() {
  destroyViewer();
  if (viewerHost) {
    const host = viewerHost;
    viewerHost = null;
    try { host.remove(); } catch (_) { /* ignore */ }
  }
  currentBuffer = null;
  currentName = '';
  firstRenderReported = false;
  window.__viewfinder = { ready: false, name: '', count: 0, gl: '', error: null, frames: 0 };
  metaEl.textContent = '空模型（等待推流）';
  setStatus('已清空，等待新的推流…');
}


function createViewer(host, bounds) {
  const f = trainingCam ? trainingCam.pose : framing(bounds);
  return new GaussianSplats3D.Viewer({
    rootElement: host,
    cameraUp: f.cameraUp,
    initialCameraPosition: f.position,
    initialCameraLookAt: f.lookAt,
    sharedMemoryForWorkers: false, // 不需要 COOP/COEP 跨源隔离头，纯本地更省事
    gpuAcceleratedSort: false,
    sceneRevealMode: GaussianSplats3D.SceneRevealMode.INSTANT,
    dynamicScene: false,
    antialiased,
    focalAdjustment,
    kernel2DSize,
    // 单个高斯的屏幕尺寸上限（单位：像素，着色器里 basisViewport 负责换算到 NDC）；
    // GS3D 默认 1024 等于不限制，过大的高斯会把画面糊住
    maxScreenSpaceSplatSize: maxScreenPx > 0 ? maxScreenPx : 1024,
    useBuiltInControls: true,
    selfDrivenMode: true,
  });
}

function reportFirstRender() {
  if (firstRenderReported) return;
  firstRenderReported = true;
  let glInfo = 'n/a';
  try {
    const gl = viewer.renderer.getContext();
    glInfo = `${gl.getParameter(gl.VERSION)} | ${gl.getParameter(gl.RENDERER)}`;
  } catch (_) { /* ignore */ }
  setStatus('已渲染');
  const dbg = { ready: true, gl: glInfo, name: currentName };
  try {
    dbg.hostConnected = !!(viewerHost && viewerHost.isConnected);
    dbg.canvas = (() => {
      const c = viewerHost && viewerHost.querySelector('canvas');
      return c ? `${c.width}x${c.height} css=${c.clientWidth}x${c.clientHeight}` : 'none';
    })();
    const cam = viewer && (viewer.camera || viewer.perspectiveCamera);
    if (cam) {
      dbg.camPos = cam.position.toArray().map((v) => +v.toFixed(2));
      dbg.camFov = +cam.fov.toFixed(2);
    }
  } catch (err) { dbg.dbgError = String(err); }
  window.__viewfinder = { ...window.__viewfinder, ...dbg };
  beacon({ event: 'firstRender', gl: glInfo, name: currentName, canvas: dbg.canvas, camPos: dbg.camPos });
}

// 可选帧数上限（?frames=N）：跑满 N 帧后停下渲染循环，便于无头截图收尾。
function capFramesAfterRender(limit) {
  if (!limit) return;
  const tick = () => {
    const state = window.__viewfinder;
    state.frames += 1;
    if (state.frames >= limit) {
      try { viewer.stop(); } catch (_) { /* ignore */ }
      beacon({ event: 'framesCapped', frames: state.frames });
      return;
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

async function display(buffer, format, name) {
  currentBuffer = buffer;
  currentFormat = format;
  currentName = name;
  firstRenderReported = false;
  window.__viewfinder = { ready: false, name, count: 0, gl: '', error: null, frames: 0 };

  let bounds = null;
  let boundsNote = '（无法解析包围盒，使用默认视角）';
  try {
    bounds = boundsFor(buffer, format);
    if (bounds) {
      const span = bounds.span.map((v) => v.toFixed(2)).join(' × ');
      const outlierRatio = bounds.fullRadius / bounds.radius;
      const outlierNote = outlierRatio > 1.2
        ? `\n全量跨度 ${bounds.fullSpan.map((v) => v.toFixed(1)).join(' × ')}（含游离高斯，已按 1%/99% 分位取景）`
        : '';
      boundsNote = `${bounds.count.toLocaleString()} 个高斯 · 稳健包围盒 ${span}（任意尺度单位）${outlierNote}`;
    }
  } catch (err) {
    setStatus(`包围盒解析跳过：${err.message}`);
  }

  window.__viewfinder.count = bounds ? bounds.count : 0;

  // 世界坐标尺寸钳制：高级选项，会改写传给 GPU 的 ply 缓冲（.splat/.ksplat 不支持）
  let uploadBuffer = buffer;
  let scaleNote = '';
  if (format === GaussianSplats3D.SceneFormat.Ply && maxScalePct > 0) {
    const maxLinear = bounds ? (maxScalePct / 100) * bounds.radius : 0;
    const t = transformPlyScales(buffer, 1.0, maxLinear);
    uploadBuffer = t.buffer;
    scaleNote = t.note
      ? `\n${t.note}`
      : `\n世界坐标尺寸上限 ${maxScalePct}% 场景半径，钳制 ${((t.clamped / 3) | 0).toLocaleString()} 个高斯`;
  }
  if (splatScale !== 1.0) scaleNote += `\n高斯尺寸 ${splatScale.toFixed(2)}x`;
  if (maxScreenPx > 0) scaleNote += `\n单个高斯屏幕尺寸上限 ${maxScreenPx}px`;
  const camNote = trainingCam
    ? `\n训练相机 id=${trainingCam.id}（${trainingCam.imgName}）· 焦长 fy=${trainingCam.fy.toFixed(0)}px 已对齐`
    : '';

  metaEl.textContent = `${name}\n${boundsNote}${scaleNote}${camNote}`;
  updateCamBar();
  setStatus('正在上传到 GPU 并渲染…');

  const prevViewer = viewer;
  destroyViewer();
  // 旧实例的宿主交给它自己的异步 dispose 收尾移除，这里只新建新宿主，彼此互不干扰。
  // 只有在"上一个宿主从未渲染出 canvas 且已无实例"时才手动清理，避免残留空 div。
  if (!prevViewer && viewerHost && !viewerHost.querySelector('canvas') && viewerHost.isConnected) {
    viewerHost.remove();
  }
  viewerHost = newViewerHost();
  viewer = createViewer(viewerHost, bounds);

  const url = URL.createObjectURL(new Blob([uploadBuffer]));
  await viewer.addSplatScene(url, {
    format,
    showLoadingUI: true,
    splatAlphaRemovalThreshold: alphaThreshold,
  });
  viewer.start();
  applyTrainingFov();
  if (viewer.splatMesh && typeof viewer.splatMesh.setSplatScale === 'function') {
    viewer.splatMesh.setSplatScale(splatScale);
  }
  URL.revokeObjectURL(url);
  requestAnimationFrame(() => requestAnimationFrame(reportFirstRender));
  capFramesAfterRender(Number(PARAMS.get('frames')) || 0);
}

/* ---------- 文件装载入口 ---------- */
function formatFromName(name) {
  const lower = name.toLowerCase();
  if (lower.endsWith('.ply')) return GaussianSplats3D.SceneFormat.Ply;
  if (lower.endsWith('.ksplat')) return GaussianSplats3D.SceneFormat.KSplat;
  if (lower.endsWith('.splat')) return GaussianSplats3D.SceneFormat.Splat;
  return null;
}

async function loadUrl(url) {
  const name = url.split('/').pop().split('?')[0];
  const format = formatFromName(name);
  if (format === null) throw new Error(`不支持的扩展名：${name}`);
  setStatus(`下载 ${name} …`);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`下载失败：HTTP ${res.status}`);
  const buffer = await res.arrayBuffer();
  await display(buffer, format, name);
}

async function loadFile(file) {
  const format = formatFromName(file.name);
  if (format === null) throw new Error(`不支持的扩展名：${file.name}`);
  setStatus(`读取 ${file.name} …`);
  const buffer = await file.arrayBuffer();
  await display(buffer, format, `${file.name}（本地拖入）`);
}

function fail(err) {
  window.__viewfinder.error = err.message;
  setStatus(`错误：${err.message}`, true);
  beacon({ event: 'error', msg: err.message });
}

/* ---------- UI 交互 ---------- */
document.getElementById('open').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) loadFile(fileInput.files[0]).catch(fail);
  fileInput.value = '';
});
document.getElementById('reset').addEventListener('click', () => {
  if (currentBuffer) display(currentBuffer, currentFormat, currentName).catch(fail);
});

/* ---------- 显示质量控件 ---------- */
const sscaleEl = document.getElementById('sscale');
const capEl = document.getElementById('cap');
const alphaEl = document.getElementById('alpha');
const aaEl = document.getElementById('aa');
const sscaleVal = document.getElementById('sscaleVal');
const capVal = document.getElementById('capVal');
const alphaVal = document.getElementById('alphaVal');
let scaleRedrawTimer = null;

function reRender() {
  if (currentBuffer) display(currentBuffer, currentFormat, currentName).catch(fail);
}

// 尺寸倍率走原生 setSplatScale，可实时生效，不必重传几何
function applyLiveScale(v) {
  splatScale = v;
  if (!currentBuffer) return;
  if (viewer && viewer.splatMesh && typeof viewer.splatMesh.setSplatScale === 'function') {
    viewer.splatMesh.setSplatScale(v);
    if (scaleRedrawTimer) clearTimeout(scaleRedrawTimer);
    scaleRedrawTimer = setTimeout(() => {
      if (currentBuffer) {
        display(currentBuffer, currentFormat, currentName).catch(fail);
      }
    }, 700);
  } else {
    reRender();
  }
}

function syncControls() {
  sscaleEl.value = String(Math.log2(splatScale));
  sscaleVal.textContent = `${splatScale.toFixed(2)}x`;
  capEl.value = String(maxScreenPx);
  capVal.textContent = maxScreenPx > 0 ? `${maxScreenPx} px` : '不限';
  alphaEl.value = String(alphaThreshold);
  alphaVal.textContent = String(alphaThreshold);
  aaEl.checked = antialiased;
}

sscaleEl.addEventListener('input', () => {
  const v = Math.pow(2, Number(sscaleEl.value));
  sscaleVal.textContent = `${v.toFixed(2)}x`;
  applyLiveScale(v);
});
capEl.addEventListener('input', () => {
  capVal.textContent = Number(capEl.value) > 0 ? `${capEl.value} px` : '不限';
});
capEl.addEventListener('change', () => {
  maxScreenPx = Number(capEl.value);
  reRender();
});
alphaEl.addEventListener('input', () => { alphaVal.textContent = alphaEl.value; });
alphaEl.addEventListener('change', () => {
  alphaThreshold = Number(alphaEl.value);
  reRender();
});
aaEl.addEventListener('change', () => {
  antialiased = aaEl.checked;
  reRender();
});

/* ---------- 面板显隐（H 键 / 按钮），截图与自动化可用 ?hud=0 ---------- */
const hudToggleEl = document.getElementById('hudToggle');
if (PARAMS.get('hud') === '0') document.body.classList.add('hud-off');

function toggleHud(force) {
  const off = force === undefined ? !document.body.classList.contains('hud-off') : force;
  document.body.classList.toggle('hud-off', off);
  if (hudToggleEl) hudToggleEl.textContent = off ? '显示面板 (H)' : '隐藏面板 (H)';
}
hudToggleEl.addEventListener('click', () => toggleHud());
document.getElementById('hudShow').addEventListener('click', () => toggleHud(false));
window.addEventListener('keydown', (e) => {
  if (e.key === 'h' || e.key === 'H') toggleHud();
});

/* ---------- 训练视角切换（有 cameras.json 时才显示） ---------- */
document.getElementById('camPrev').addEventListener('click', () => stepCamera(-1));
document.getElementById('camNext').addEventListener('click', () => stepCamera(1));
window.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft') stepCamera(-1);
  if (e.key === 'ArrowRight') stepCamera(1);
});

let dragDepth = 0;
window.addEventListener('dragenter', (e) => {
  e.preventDefault();
  dragDepth += 1;
  dropEl.classList.add('on');
});
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('dragleave', (e) => {
  e.preventDefault();
  dragDepth -= 1;
  if (dragDepth <= 0) { dragDepth = 0; dropEl.classList.remove('on'); }
});
window.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  dropEl.classList.remove('on');
  const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) loadFile(file).catch(fail);
});

/* ---------- M6.0c 模型切换 + 下载（原始模型 vs 处理模型） ---------- */
const MODELS = {
  orig: { name: '原始模型（M0→M5）', url: 'splats/m5_original.ply', cams: 'splats/m5_cameras.json' },
  proc: { name: '除雾模型', url: 'splats/m6_processed.ply', cams: 'splats/m5_cameras.json' },
};

async function fileExists(url) {
  try {
    const r = await fetch(url, { method: 'HEAD' });
    return r.ok;
  } catch (_) { return false; }
}

let procReady = false;

async function refreshModelBar() {
  const btn = document.getElementById('modelProc');
  const dlP = document.getElementById('dlProc');
  const dlO = document.getElementById('dlOrig');
  procReady = await fileExists(MODELS.proc.url);
  if (btn) {
    // 不 disable：未完成时也要允许点击并给出提示（见 switchModel）
    btn.textContent = procReady ? '除雾模型' : '除雾模型（未完成）';
  }
  if (dlP) dlP.classList.toggle('disabled', !procReady);
  if (dlO) dlO.classList.toggle('disabled', !(await fileExists(MODELS.orig.url)));
}

/* ---------- 云 case 选择 ---------- */
const CASES_URL = 'http://localhost:8765/cases';
let selectedCase = null;

async function refreshCases() {
  const sel = document.getElementById('caseSel');
  try {
    const res = await fetch(CASES_URL);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();
    sel.innerHTML = '<option value="">本地默认</option>';
    for (const c of list) {
      const o = document.createElement('option');
      o.value = c.ts;
      const dt = c.dehaze === 'done' ? ' 除雾✓' : (c.dehaze === 'running' ? ' 除雾中' : '');
      o.textContent = `${c.ts}${dt}`;
      sel.appendChild(o);
    }
    setStatus(`云 case ${list.length} 个`);
    beacon({ event: 'casesLoaded', n: list.length });
  } catch (_) {
    sel.innerHTML = '<option value="">本地默认（无云连接）</option>';
    setStatus('未连接云 case 服务（需 SSH 隧道 8765），用本地默认', true);
  }
}
document.getElementById('caseRefresh').addEventListener('click', () => refreshCases().catch(() => {}));
document.getElementById('caseDelete').addEventListener('click', async () => {
  const ts = document.getElementById('caseSel').value;
  if (!ts) { setStatus('未选择云 case，无法删除', true); return; }
  if (!window.confirm(`确认删除云 case ${ts}？此操作不可恢复（含原始/除雾/取景结果）。`)) return;
  try {
    const res = await fetch(`${CASES_URL}?case=${encodeURIComponent(ts)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    setStatus(`已删除 case ${ts}`);
    beacon({ event: 'caseDeleted', case: ts });
    refreshCases().catch(() => {});
  } catch (e) {
    setStatus(`删除失败：${e.message}`, true);
  }
});
document.getElementById('caseSel').addEventListener('change', (e) => {
  selectedCase = e.target.value || null;
  setStatus(selectedCase ? `已选云 case ${selectedCase}，点模型按钮加载` : '使用本地默认模型');
});

async function switchModel(key) {
  const m = MODELS[key];
  if (key === 'proc' && !selectedCase) {
    // 本地模式：点击时重新探测（构建完成后无需刷新即可切换）
    procReady = await fileExists(m.url);
    if (!procReady) {
      setStatus('除雾模型尚未完成（构建中），请稍后再试', true);
      beacon({ event: 'modelNotReady', key });
      return;
    }
    refreshModelBar();
  }
  trainingCam = null;
  camerasList = null;
  camIndex = 0;

  if (selectedCase) {
    // 云 case 模式
    const model = key === 'proc' ? 'dehaze' : 'orig';
    const url = `http://localhost:8765/ply?case=${selectedCase}&model=${model}`;
    setStatus(`加载 case ${selectedCase} ${m.name} …`);
    beacon({ event: 'modelSwitch', key, case: selectedCase });
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const buf = await res.arrayBuffer();
    try {
      trainingCam = await loadTrainingCamera(
        `http://localhost:8765/cameras?case=${selectedCase}`, PARAMS.get('kf'));
    } catch (_) { /* 无相机则自动框景 */ }
    await display(buf, GaussianSplats3D.SceneFormat.Ply, `${m.name}（case ${selectedCase}）`);
    return;
  }

  if (m.cams) {
    try {
      trainingCam = await loadTrainingCamera(m.cams, PARAMS.get('kf'));
      beacon({ event: 'modelCam', key, id: trainingCam.id });
    } catch (_) { /* 无相机则用自动框景 */ }
  }
  setStatus(`切换到 ${m.name} …`);
  beacon({ event: 'modelSwitch', key });
  await loadUrl(m.url);
}

document.getElementById('modelOrig').addEventListener('click', () => switchModel('orig').catch(fail));
document.getElementById('modelProc').addEventListener('click', () => switchModel('proc').catch(fail));

/* ---------- 帮我构图：按 case 拉取/生成 top-3 ---------- */
const framingPanel = document.getElementById('framingPanel');
const framingList = document.getElementById('framingList');
let pendingFramingCase = null;   // 正在生成的 case，供 WS framing_done 回调
function closeFraming() { framingPanel.classList.remove('on'); }
document.getElementById('framingClose').addEventListener('click', closeFraming);
framingPanel.addEventListener('click', (e) => { if (e.target === framingPanel) closeFraming(); });

function renderFraming(data, ts) {
  if (!data.top3 || !data.top3.length) throw new Error('无 top-3 数据');
  framingList.innerHTML = '';
  const note = document.createElement('div');
  note.className = 'fnote';
  note.textContent = ts
    ? `以下为 case ${ts} 的取景推荐（Top-3）。`
    : '以下为固定演示推荐（旧 s0 case，与所选 case 无关）。';
  framingList.appendChild(note);
  data.top3.forEach((t, i) => {
    const div = document.createElement('div');
    div.className = 'frec';
    const img = document.createElement('img');
    img.src = ts
      ? `http://localhost:8765/framing-image?case=${ts}&name=${t.name}.png`
      : `framing/${t.name}.png`;
    img.alt = t.name;
    img.loading = 'lazy';
    const txt = document.createElement('div');
    txt.className = 'txt';
    const r = String(t.reason || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    txt.innerHTML = `<b>Top ${i + 1} · ${t.name}</b><br>${r}`;
    div.appendChild(img);
    div.appendChild(txt);
    framingList.appendChild(div);
  });
  framingPanel.classList.add('on');
}

async function showFraming() {
  setStatus('加载取景推荐…');
  beacon({ event: 'framingOpen', case: selectedCase || '' });
  const ts = selectedCase;

  if (!ts) {
    // 本地默认：固定演示数据
    try {
      const res = await fetch('framing/framing.json');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      renderFraming(await res.json(), null);
      setStatus('已显示构图建议 Top-3');
    } catch (err) {
      setStatus(`构图建议加载失败：${err.message}`, true);
      beacon({ event: 'framingError', msg: err.message });
    }
    return;
  }

  // 云 case 模式：先查已有，没有再触发生成
  try {
    const res = await fetch(`http://localhost:8765/framing?case=${ts}`);
    if (res.ok) {
      renderFraming(await res.json(), ts);
      setStatus(`已显示 case ${ts} 取景推荐 Top-3`);
      return;
    }
    if (res.status === 404) {
      const gen = await fetch(`http://localhost:8765/framing?case=${ts}`, { method: 'POST' });
      const gs = await gen.json().catch(() => ({}));
      if (gs.status === 'done') {
        const r2 = await fetch(`http://localhost:8765/framing?case=${ts}`);
        renderFraming(await r2.json(), ts);
        setStatus(`已显示 case ${ts} 取景推荐 Top-3`);
        return;
      }
      // 生成中 → 显示进度，等 WS framing_done 自动刷新
      pendingFramingCase = ts;
      framingPanel.classList.add('on');
      framingList.innerHTML = '';
      const note = document.createElement('div');
      note.className = 'fnote';
      note.textContent = `⏳ 正在生成 case ${ts} 的取景推荐（预计约 10–15 分钟），完成后自动刷新…`;
      framingList.appendChild(note);
      setStatus('取景推荐生成中（预计约 10–15 分钟），完成后自动刷新');
      beacon({ event: 'framingGenerating', case: ts });
      return;
    }
    throw new Error(`HTTP ${res.status}`);
  } catch (err) {
    setStatus(`取景推荐加载失败：${err.message}`, true);
    beacon({ event: 'framingError', msg: err.message });
  }
}
document.getElementById('framingBtn').addEventListener('click', showFraming);

/* ---------- M7 增量推流：累计表 + 本地 PLY 重建（Phase A） ----------
 * 云端推「id-indexed 全量同步(0x00) + 增量 delta(0x01)」，浏览器维护 {id → 62 float} 累计表，
 * 节流合并后本地重建为完整 PLY 再 display()。旧协议（裸 ply，首字节 'p'=0x70）走整幅重载，向后兼容。
 * 62 float 布局与 Photo-SLAM savePly 完全一致：
 *   xyz(3) + nx,ny,nz(3,恒0) + f_dc(3) + f_rest(45) + opacity(1) + scale(3) + rot_quat(4)
 */
const PLY_N_FLOATS = 62;
const ACCUM_REBUILD_MS = 500;   // delta 到达后的合并重建节流窗口
let accumMap = null;            // Map<id, Float32Array(62)>；null = 未启用（旧整幅重载模式）
let accumRebuildTimer = null;
let lastM7Iter = 0;             // 最近一次增量/全量对应的训练迭代号（云端 m7_iter 消息）

const PLY_FIELDS = [
  'x', 'y', 'z', 'nx', 'ny', 'nz',
  'f_dc_0', 'f_dc_1', 'f_dc_2',
  ...Array.from({ length: 45 }, (_, i) => `f_rest_${i}`),
  'opacity', 'scale_0', 'scale_1', 'scale_2', 'rot_0', 'rot_1', 'rot_2', 'rot_3',
];

function plyHeaderText(count) {
  const props = PLY_FIELDS.map((f) => `property float ${f}`).join('\n');
  return `ply\nformat binary_little_endian 1.0\nelement vertex ${count}\n${props}\nend_header\n`;
}

// 从累计表生成完整 PLY（顶点按 id 升序，确定性输出；头部与 Photo-SLAM savePly 逐字段一致）
function buildPlyFromAccum() {
  const ids = Array.from(accumMap.keys()).sort((a, b) => a - b);
  const count = ids.length;
  const headerBytes = new TextEncoder().encode(plyHeaderText(count));
  const buf = new ArrayBuffer(headerBytes.byteLength + count * PLY_N_FLOATS * 4);
  new Uint8Array(buf, 0, headerBytes.byteLength).set(headerBytes);
  const dv = new DataView(buf, headerBytes.byteLength);
  let off = 0;
  for (const id of ids) {
    const p = accumMap.get(id);
    for (let k = 0; k < PLY_N_FLOATS; k++) dv.setFloat32(off + k * 4, p[k], true);
    off += PLY_N_FLOATS * 4;
  }
  return buf;
}

function rebuildFromAccum() {
  if (!accumMap) return;
  const n = accumMap.size;
  const iterNote = lastM7Iter > 0 ? ` · 迭代 ${lastM7Iter.toLocaleString()}` : '';
  display(buildPlyFromAccum(), GaussianSplats3D.SceneFormat.Ply,
    `实时快照（增量累计 ${n.toLocaleString()} 高斯${iterNote}）`).catch(fail);
}

function scheduleAccumRebuild() {
  if (accumRebuildTimer) clearTimeout(accumRebuildTimer);
  accumRebuildTimer = setTimeout(() => {
    accumRebuildTimer = null;
    rebuildFromAccum();
  }, ACCUM_REBUILD_MS);
}

// id-indexed 全量同步：u32 count + count×(i64 id + 62 f32)
function parseFullSync(buf) {
  const dv = new DataView(buf);
  const count = dv.getUint32(0, true);
  let off = 4;
  const entries = [];
  for (let i = 0; i < count; i++) {
    const id = Number(dv.getBigInt64(off, true)); off += 8;
    const p = new Float32Array(PLY_N_FLOATS);
    for (let k = 0; k < PLY_N_FLOATS; k++) { p[k] = dv.getFloat32(off, true); off += 4; }
    entries.push({ id, params: p });
  }
  return entries;
}

// 增量 delta：u32 added + u32 removed + added×(i64 id + 62 f32) + removed×i64
function parseDelta(buf) {
  const dv = new DataView(buf);
  let off = 0;
  const addedN = dv.getUint32(off, true); off += 4;
  const removedN = dv.getUint32(off, true); off += 4;
  const added = [];
  for (let i = 0; i < addedN; i++) {
    const id = Number(dv.getBigInt64(off, true)); off += 8;
    const p = new Float32Array(PLY_N_FLOATS);
    for (let k = 0; k < PLY_N_FLOATS; k++) { p[k] = dv.getFloat32(off, true); off += 4; }
    added.push({ id, params: p });
  }
  const removed = [];
  for (let i = 0; i < removedN; i++) { removed.push(Number(dv.getBigInt64(off, true))); off += 8; }
  return { added, removed };
}

function applyFullSync(buf) {
  // 先完整解析、成功后才重置累计表：若消息截断（云端写文件瞬间被读到半截），
  // 不破坏现有累计表，等下一条周期全量兜底纠正。
  let entries;
  try { entries = parseFullSync(buf); }
  catch (err) {
    beacon({ event: 'fullSyncParseError', msg: String(err && err.message || err) });
    return;
  }
  accumMap = new Map();
  for (const e of entries) accumMap.set(e.id, e.params);
  beacon({ event: 'fullSync', count: accumMap.size });
  rebuildFromAccum();
}

function applyDelta(buf) {
  if (!accumMap) {
    // 尚无基线（未收到全量同步）就来了 delta：丢弃；服务端连接时应先推全量
    beacon({ event: 'deltaWithoutBaseline' });
    return;
  }
  let d;
  try { d = parseDelta(buf); }
  catch (err) {
    beacon({ event: 'deltaParseError', msg: String(err && err.message || err) });
    return;
  }
  for (const e of d.added) accumMap.set(e.id, e.params);
  for (const id of d.removed) accumMap.delete(id);
  beacon({ event: 'delta', added: d.added.length, removed: d.removed.length, total: accumMap.size });
  scheduleAccumRebuild();
}

function handleBinaryMessage(buf) {
  const tag = buf.byteLength > 0 ? new DataView(buf).getUint8(0) : -1;
  try {
    if (tag === 0x00) applyFullSync(buf.slice(1));
    else if (tag === 0x01) applyDelta(buf.slice(1));
    else {
      // 旧协议裸 ply（首字节 'p'=0x70）：整幅重载，回到无 id 基线
      accumMap = null;
      display(buf, GaussianSplats3D.SceneFormat.Ply, '实时快照（WebSocket）').catch(fail);
    }
  } catch (err) {
    // 兜底：任何同步解析异常都不该把 WS 消息处理打断
    beacon({ event: 'm7ParseError', msg: String(err && err.message || err) });
  }
}

// 供无头验证 / 调试用（见 tools/verify-web-viewer.mjs）
window.__m7 = {
  applyFullSync, applyDelta, buildPlyFromAccum, parseDelta, parseFullSync,
  accum: () => accumMap,
};

/* ---------- 实时 WebSocket（?live=ws://host:port 启用，二进制 ply 快照） ---------- */
function connectLive() {
  const wsUrl = PARAMS.get('live');
  if (!wsUrl) return;

  // 推流控制按钮（?live= 时显示）
  const liveBar = document.getElementById('liveBar');
  if (liveBar) liveBar.style.display = '';
  document.getElementById('liveStart').addEventListener('click', () => {
    if (liveWs && liveWs.readyState === 1) {
      liveWs.send('start');
      setStatus('已发送「开始推流」→ 先静止 2–3s，再缓慢扫动');
      beacon({ event: 'liveStart' });
    } else {
      setStatus('实时流未连接，稍候…', true);
    }
  });
  document.getElementById('liveStop').addEventListener('click', () => {
    if (liveWs && liveWs.readyState === 1) {
      liveWs.send('stop');
      setStatus('已发送「停止推流」→ 进入后台精修');
      beacon({ event: 'liveStop' });
    } else {
      setStatus('实时流未连接，稍候…', true);
    }
  });
  document.getElementById('liveRestart').addEventListener('click', () => {
    clearDisplay();
    if (liveWs && liveWs.readyState === 1) {
      liveWs.send('start');
      setStatus('已重新开始：清空显示 + 云端重新推流');
      beacon({ event: 'liveRestart' });
    } else {
      setStatus('已清空显示，但实时流未连接', true);
    }
  });

  let reconnectDelay = 1000;
  function connect() {
    let ws;
    try { ws = new WebSocket(wsUrl); } catch (_) { setTimeout(connect, reconnectDelay); return; }
    liveWs = ws;
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      reconnectDelay = 1000;
      setStatus('已连接实时流，等待快照…');
      beacon({ event: 'liveConnected', url: wsUrl });
    };
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        handleBinaryMessage(ev.data);
      } else if (typeof ev.data === 'string') {
        if (ev.data === 'final') {
          setStatus('✅ 原始模型（M0→M5）已完成建模，开始除雾…');
          beacon({ event: 'liveFinal' });
        } else if (ev.data.startsWith('{') && ev.data.includes('"type":"m7_iter"')) {
          try {
            const s = JSON.parse(ev.data);
            lastM7Iter = Number(s.iter) || 0;
            beacon({ event: 'm7Iter', kind: s.kind, iter: lastM7Iter });
          } catch (_) { /* ignore */ }
        } else if (ev.data.startsWith('{') && ev.data.includes('"type":"status"')) {
          try {
            const s = JSON.parse(ev.data);
            if (s.phase === 'framing_done' && s.case && s.case === pendingFramingCase) {
              pendingFramingCase = null;
              setStatus(`✅ case ${s.case} 取景推荐已生成`);
              beacon({ event: 'framingDone', case: s.case });
              fetch(`http://localhost:8765/framing?case=${s.case}`)
                .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
                .then(data => { renderFraming(data, s.case); setStatus(`已显示 case ${s.case} 取景推荐 Top-3`); })
                .catch(e => setStatus(`取景推荐刷新失败：${e.message}`, true));
              return;
            }
            if (s.phase === 'framing_error' && s.case === pendingFramingCase) {
              pendingFramingCase = null;
              setStatus(`取景推荐生成失败（case ${s.case}），请查看云端 /tmp/framing_${s.case}.log`, true);
              return;
            }
            const txt = {
              modeling_done: '✅ 原始模型（M0→M5）已完成建模',
              dehazing: '🔄 正在除雾（除雾模型）…',
              dehaze_done: `✅ 除雾模型已完成（case ${s.case || ''}）`,
              dehaze_failed: `⚠️ 除雾失败，原始模型仍可查看（case ${s.case || ''}）`,
            }[s.phase];
            if (txt) setStatus(txt);
            beacon({ event: 'status', phase: s.phase, case: s.case || '' });
          } catch (_) { /* ignore */ }
        }
      }
    };
    ws.onclose = () => {
      setStatus(`实时流断开，${Math.round(reconnectDelay / 1000)}s 后重连…`, true);
      beacon({ event: 'liveClosed' });
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }
  connect();
}

/* ---------- 启动 ---------- */
(async () => {
  beacon({ event: 'pageReady', ua: navigator.userAgent.slice(0, 120) });
  syncControls();
  refreshModelBar().catch(() => {});
  refreshCases().catch(() => {});
  const params = new URLSearchParams(location.search);
  const url = params.get('url') || DEFAULT_SPLAT;
  if (params.get('cam') && params.get('cam') !== '0') {
    try {
      trainingCam = await loadTrainingCamera(params.get('cam'), params.get('kf'));
      beacon({ event: 'trainingCam', id: trainingCam.id, img: trainingCam.imgName, fy: trainingCam.fy });
    } catch (err) {
      setStatus(`训练相机加载失败：${err.message}`, true);
      beacon({ event: 'error', msg: `training camera: ${err.message}` });
    }
  } else if (params.get('cam') !== '0') {
    // 自动尝试同目录 cameras.json：默认给出训练视角
    try {
      trainingCam = await tryAutoCameras(url);
      if (trainingCam) {
        beacon({ event: 'trainingCam', id: trainingCam.id, img: trainingCam.imgName, auto: '1' });
      }
    } catch (_) { /* 没有就退回自动框景 */ }
  }
  if (PARAMS.get('live')) {
    // 实时模式：跳过默认场景，等 WebSocket 推送快照
    connectLive();
  } else {
    try {
      await loadUrl(url);
    } catch (err) {
      setStatus(`加载默认场景失败：${err.message}（可拖入本地文件）`, true);
      beacon({ event: 'error', msg: `default load failed: ${err.message}` });
      metaEl.textContent = '未加载场景\n可拖入本地 .ply 文件';
    }
  }
})();

// 视口变化时重新对齐训练焦长（focal = 视口高/2 / tan(fov/2)）
let resizeTimer = null;
window.addEventListener('resize', () => {
  if (!trainingCam) return;
  if (resizeTimer) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(applyTrainingFov, 150);
});
