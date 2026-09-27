#!/usr/bin/env node
/*
 * 无头验证工具：用 CDP 驱动 Chrome 打开本地 3DGS 查看器，
 * 等待 window.__viewfinder.ready，抓取控制台异常，并截一张图。
 *
 * 用途：改完查看器代码后确认「页面能加载、PLY 能解析、WebGL 能出帧」，
 * 不依赖人眼看图；后续做动态 splat 增量更新时也可以直接复用。
 *
 * 用法:
 *   node verify-web-viewer.mjs [--url http://127.0.0.1:8099/?frames=45]
 *                              [--out _smoke/viewer_shot.png] [--angle metal|swiftshader]
 *                              [--timeout 60000] [--keep-open]
 */
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const flag = (name) => process.argv.includes(`--${name}`);

const URL_TO_TEST = arg('url', 'http://127.0.0.1:8099/?frames=45');
const OUT = path.resolve(HERE, arg('out', '_smoke/viewer_shot.png'));
const ANGLE = arg('angle', 'metal');
const TIMEOUT = Number(arg('timeout', '90000'));
const PORT = Number(arg('port', '9222'));
const PROFILE = path.join(HERE, '_smoke', 'chrome-cdp-profile');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const chromeArgs = [
  '--headless=new',
  '--no-sandbox', '--disable-gpu-sandbox',
  '--disable-crash-reporter', '--disable-breakpad',
  `--crash-dumps-dir=${path.join(HERE, '_smoke', 'crash')}`,
  '--no-first-run', '--no-default-browser-check', '--disable-extensions',
  '--hide-scrollbars', '--mute-audio',
  `--user-data-dir=${PROFILE}`,
  `--remote-debugging-port=${PORT}`,
  `--window-size=${arg('size', '1280,800')}`,
  `--use-angle=${ANGLE}`,
  '--enable-unsafe-swiftshader',
  'about:blank',
];

await fs.rm(PROFILE, { recursive: true, force: true });
await fs.mkdir(PROFILE, { recursive: true });
await fs.mkdir(path.dirname(OUT), { recursive: true });

console.log(`[verify] chrome: ${CHROME}`);
console.log(`[verify] url   : ${URL_TO_TEST}`);
console.log(`[verify] angle : ${ANGLE}`);

const chrome = spawn(CHROME, chromeArgs, { stdio: ['ignore', 'ignore', 'pipe'] });
const chromeErr = [];
chrome.stderr.on('data', (b) => chromeErr.push(b.toString()));

const cleanup = () => { try { chrome.kill('SIGKILL'); } catch { /* ignore */ } };
process.on('exit', cleanup);

// --- 等 DevTools 端口 ---
let version = null;
for (let i = 0; i < 100; i++) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/version`);
    if (res.ok) { version = await res.json(); break; }
  } catch { /* not up yet */ }
  await sleep(200);
}
if (!version) {
  console.error('[verify] FAIL: DevTools 端口未就绪');
  console.error(chromeErr.join('').split('\n').filter((l) => /error|fail/i.test(l)).slice(0, 8).join('\n'));
  cleanup();
  process.exit(1);
}
console.log(`[verify] browser: ${version.Browser}`);

// --- 新建标签页（Chrome 111+ 需要 PUT） ---
let target = null;
try {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(URL_TO_TEST)}`, { method: 'PUT' });
  if (res.ok) target = await res.json();
} catch { /* fall through */ }
if (!target) {
  console.error('[verify] FAIL: 无法创建标签页');
  cleanup();
  process.exit(1);
}

// --- 接上 CDP ---
const ws = new WebSocket(target.webSocketDebuggerUrl);
let nextId = 1;
const pending = new Map();
const consoleErrors = [];
const exceptions = [];

const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject, method });
  ws.send(JSON.stringify({ id, method, params }));
});

ws.addEventListener('message', (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject, method } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(`${method}: ${msg.error.message}`));
    else resolve(msg.result);
    return;
  }
  if (msg.method === 'Runtime.consoleAPICalled') {
    const text = (msg.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(' ');
    if (msg.params.type === 'error') consoleErrors.push(text);
    else console.log(`[page:${msg.params.type}] ${text}`);
  }
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params.exceptionDetails;
    exceptions.push(d.exception?.description || d.text);
  }
});

await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true });
  ws.addEventListener('error', () => reject(new Error('CDP WebSocket 连接失败')), { once: true });
});
await send('Page.enable');
await send('Runtime.enable');

// --- 等页面报告渲染完成 ---
const deadline = Date.now() + TIMEOUT;
let state = null;
while (Date.now() < deadline) {
  try {
    const r = await send('Runtime.evaluate', {
      expression: 'JSON.stringify(window.__viewfinder || null)',
      returnByValue: true,
    });
    const raw = r.result?.value;
    if (raw) {
      state = JSON.parse(raw);
      if (state.ready || state.error) break;
    }
  } catch { /* 导航中，稍后再试 */ }
  await sleep(400);
}

const report = { url: URL_TO_TEST, state, consoleErrors, exceptions };
console.log(`[verify] state: ${JSON.stringify(state)}`);

// 可选：页面就绪后执行一段 JS（用于测试按钮/交互），然后再等一次渲染完成
const JS = arg('js', '');
if (JS && state && state.ready) {
  console.log(`[verify] eval: ${JS}`);
  try {
    const r = await send('Runtime.evaluate', { expression: JS, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) console.error(`[verify] eval exception: ${r.exceptionDetails.text}`);
    console.log(`[verify] eval result: ${JSON.stringify(r.result?.value)}`);
  } catch (err) {
    console.error(`[verify] eval failed: ${err.message}`);
  }
  await sleep(Number(arg('after', '3000')));
  try {
    const r = await send('Runtime.evaluate', {
      expression: 'JSON.stringify(window.__viewfinder || null)',
      returnByValue: true,
    });
    state = JSON.parse(r.result?.value);
    report.stateAfterJs = state;
    report.camLabel = (await send('Runtime.evaluate', {
      expression: "(document.getElementById('camLabel')||{}).textContent || null",
      returnByValue: true,
    })).result?.value;
    console.log(`[verify] state after js: ${JSON.stringify(state)}  camLabel=${report.camLabel}`);
  } catch (err) {
    console.error(`[verify] post-eval state read failed: ${err.message}`);
  }
}

if (state && state.ready) {
  // 让最后一帧稳定后再截图
  await sleep(1200);
  try {
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    await fs.writeFile(OUT, Buffer.from(shot.data, 'base64'));
    const { size } = await fs.stat(OUT);
    report.screenshot = { path: OUT, bytes: size };
    console.log(`[verify] screenshot: ${OUT} (${(size / 1024).toFixed(0)} KB)`);
  } catch (err) {
    report.screenshotError = err.message;
    console.error(`[verify] screenshot failed: ${err.message}`);
  }
}

if (exceptions.length) {
  console.error('[verify] page exceptions:');
  for (const e of exceptions.slice(0, 5)) console.error(`  - ${e}`);
}
if (consoleErrors.length) {
  console.error('[verify] console errors:');
  for (const e of consoleErrors.slice(0, 5)) console.error(`  - ${e}`);
}

await fs.writeFile(path.join(HERE, '_smoke', 'verify-report.json'), JSON.stringify(report, null, 2));
console.log(`[verify] report -> ${path.join(HERE, '_smoke', 'verify-report.json')}`);

const ok = Boolean(state && state.ready && !state.error && exceptions.length === 0);
console.log(ok ? '[verify] RESULT: PASS' : '[verify] RESULT: FAIL');

if (!flag('keep-open')) cleanup();
await sleep(300);
process.exit(ok ? 0 : 2);
