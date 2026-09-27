// M4.4+M5.2+M6.0c: 云端服务 —— 监控快照 ply 推送二进制 + start/stop 控制 +
// 完成后归档 case + 自动除雾 + 状态广播 + HTTP 接口（/cases 列表、/ply 拉模型）
// 用法：node server.js <snapshot.ply路径> [端口]
const fs = require('fs');
const http = require('http');
const path = require('path');
const { WebSocketServer } = require('ws');
const { exec, execSync } = require('child_process');
const { attach: attachM7 } = require('./m7_ws_delta.js');   // M7.2 增量推流

const SNAPSHOT_PATH = process.argv[2] || '/root/Photo-SLAM/results/m5_live/live/snapshot.ply';
const PORT = Number(process.argv[3]) || 8765;
const CASES_DIR = '/root/Photo-SLAM/results/cases';
const framingJobs = {};   // { <ts>: { startedAt } } 取景推荐生成中任务

// ---------- HTTP：/cases 列表 + /ply 拉模型 ----------
const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/cases' && req.method === 'DELETE') {
    const ts = url.searchParams.get('case');
    if (!ts || !/^[\w-]+$/.test(ts)) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'bad case' }));
      return;
    }
    const dir = path.join(CASES_DIR, ts);
    if (!fs.existsSync(dir)) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'not found' }));
      return;
    }
    try {
      fs.rmSync(dir, { recursive: true, force: true });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ deleted: ts }));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: String(e) }));
    }
    return;
  }
  if (url.pathname === '/cases') {
    const out = [];
    try {
      for (const d of fs.readdirSync(CASES_DIR)) {
        let m = {};
        try { m = JSON.parse(fs.readFileSync(path.join(CASES_DIR, d, 'manifest.json'), 'utf8')); }
        catch (_) { /* 无 manifest 则留空 */ }
        out.push({ ts: d, original: m.original || '', dehaze: m.dehaze || '' });
      }
    } catch (_) { /* cases 目录不存在 */ }
    out.sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(out));
    return;
  }
  if (url.pathname === '/ply') {
    const ts = url.searchParams.get('case');
    const model = url.searchParams.get('model') || 'orig';
    if (!ts) { res.writeHead(400, { 'Content-Type': 'text/plain' }); res.end('missing case'); return; }
    const p = model === 'dehaze'
      ? path.join(CASES_DIR, ts, 'dehazed', 'dehazed.ply')
      : path.join(CASES_DIR, ts, 'original', 'point_cloud.ply');
    if (!fs.existsSync(p)) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('not found: ' + p);
      return;
    }
    res.writeHead(200, {
      'Content-Type': 'application/octet-stream',
      'Content-Length': fs.statSync(p).size,
      'Cache-Control': 'no-cache',
    });
    fs.createReadStream(p).pipe(res);
    return;
  }
  if (url.pathname === '/cameras') {
    const ts = url.searchParams.get('case');
    if (!ts) { res.writeHead(400, { 'Content-Type': 'text/plain' }); res.end('missing case'); return; }
    const p = path.join(CASES_DIR, ts, 'original', 'cameras.json');
    if (!fs.existsSync(p)) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('not found: ' + p);
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-cache' });
    fs.createReadStream(p).pipe(res);
    return;
  }
  // ---------- 取景推荐 ----------
  if (url.pathname === '/framing' && req.method === 'POST') {
    const ts = url.searchParams.get('case');
    if (!ts || !/^[\w-]+$/.test(ts)) { res.writeHead(400, { 'Content-Type': 'text/plain' }); res.end('bad case'); return; }
    const fp = path.join(CASES_DIR, ts, 'framing', 'framing.json');
    if (fs.existsSync(fp)) {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'done' }));
      return;
    }
    if (framingJobs[ts]) {
      res.writeHead(202, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'generating' }));
      return;
    }
    framingJobs[ts] = { startedAt: Date.now() };
    const log = `/tmp/framing_${ts}.log`;
    exec(`nohup setsid bash ~/m6/framing_for_case.sh ${ts} > ${log} 2>&1 < /dev/null &`, (err) => {
      if (err) console.log(`[framing] start err: ${err}`);
    });
    console.log(`[framing] start case ${ts} -> ${log}`);
    res.writeHead(202, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ status: 'generating' }));
    return;
  }
  if (url.pathname === '/framing') {
    const ts = url.searchParams.get('case');
    if (!ts || !/^[\w-]+$/.test(ts)) { res.writeHead(400, { 'Content-Type': 'text/plain' }); res.end('bad case'); return; }
    const fp = path.join(CASES_DIR, ts, 'framing', 'framing.json');
    if (fs.existsSync(fp)) {
      res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-cache' });
      fs.createReadStream(fp).pipe(res);
      return;
    }
    if (framingJobs[ts]) {
      res.writeHead(202, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'generating' }));
      return;
    }
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('not generated');
    return;
  }
  if (url.pathname === '/framing-image') {
    const ts = url.searchParams.get('case');
    const name = url.searchParams.get('name');
    if (!ts || !name) { res.writeHead(400, { 'Content-Type': 'text/plain' }); res.end('missing params'); return; }
    if (!/^[\w.-]+\.png$/.test(name)) { res.writeHead(400, { 'Content-Type': 'text/plain' }); res.end('bad name'); return; }
    const p = path.join(CASES_DIR, ts, 'framing', name);
    if (!fs.existsSync(p)) { res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('not found'); return; }
    res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-cache' });
    fs.createReadStream(p).pipe(res);
    return;
  }
  res.writeHead(404, { 'Content-Type': 'text/plain' });
  res.end('not found');
});

const wss = new WebSocketServer({ server });
let lastMtime = 0;
let lastSize = -1;
let pushedOnce = false;
let finalSent = false;

// M7.2: 增量推流（delta + id-indexed 全量），取代下方「裸 snapshot.ply 整幅推送」。
// onPush 表示「本次采集产出了新数据」，驱动下方 final 检测的 pushedOnce 判定。
const m7 = attachM7({
  wss,
  // deltas 在 mapper 的 result_dir/deltas/（与 live/ 平级），snapshot 在 result_dir/live/snapshot.ply
  deltasDir: path.join(path.dirname(path.dirname(SNAPSHOT_PATH)), 'deltas'),
  fullThrottleMs: 15000,
  onPush: () => { pushedOnce = true; },
  onReset: () => { pushedOnce = false; finalSent = false; },   // 新采集：重置「已发过 final」判定
});

// 启动时以当前 snapshot 为基线：避免「重启即误判快照变化」而重复触发 finalize。
try {
  const st0 = fs.statSync(SNAPSHOT_PATH);
  lastMtime = st0.mtimeMs;
  lastSize = st0.size;
} catch (_) { /* 尚无快照，保持 0 / -1 */ }

function readSnapshot() {
  try { return fs.readFileSync(SNAPSHOT_PATH); } catch (_) { return null; }
}

function broadcast(data) {
  let n = 0;
  for (const client of wss.clients) {
    if (client.readyState === 1) { client.send(data); n++; }
  }
  return n;
}

function broadcastStatus(phase, extra) {
  broadcast(JSON.stringify(Object.assign({ type: 'status', phase }, extra || {})));
}

// 放弃未完成的 case：终止正在跑的归档/除雾进程 + 删除 manifest 未完成的 case 目录。
// 用于「开始推流/重新开始」时清理上一轮残留（如误归档的旧数据、卡在除雾中的 case）。
function abandonIncompleteCases() {
  // 1) 终止归档/除雾进程（字符类打断字面匹配，避免 pkill 自匹配本 sh -c 包装）
  try {
    execSync('pkill -f "finalize_cas[e]|auto_dehaz[e]|prune_floater[s]|compare_model[s]" || true', { stdio: 'ignore' });
  } catch (_) { /* ignore */ }
  // 2) 删除未完成的 case（original/dehaze 任一非 done）
  try {
    for (const d of fs.readdirSync(CASES_DIR)) {
      let m = {};
      try { m = JSON.parse(fs.readFileSync(path.join(CASES_DIR, d, 'manifest.json'), 'utf8')); }
      catch (_) { continue; }
      if (m.original !== 'done' || m.dehaze !== 'done') {
        try {
          fs.rmSync(path.join(CASES_DIR, d), { recursive: true, force: true });
          console.log(`[abandon] 丢弃未完成 case ${d}`);
        } catch (_) { /* ignore */ }
      }
    }
  } catch (_) { /* cases 目录不存在 */ }
}

wss.on('connection', (ws) => {
  console.log(`[conn] +1 (total ${wss.clients.size})`);

  ws.on('message', (msg) => {
    const text = String(msg).trim();
    if (text === 'start') {
      console.log('[ctrl] start');
      // M7 修复：开始新采集前，立即阻断 final 判定并放弃未完成 case。
      // 否则 m4_start.sh 的 pkill→rm 窗口里（tum_mono 短暂不存在），final 检测
      // 会把上一轮残留数据误归档成一个新 case（见 case 20260927_130830 事故）。
      // 等 m7 模块 detectReset（新采集清空目录）时经 onReset 放行 finalSent。
      pushedOnce = false;
      finalSent = true;
      abandonIncompleteCases();
      exec('bash ~/m4_start.sh', (err, so, se) => {
        console.log(`[ctrl] start done: ${(so || se || '').trim()}`);
        if (err) console.log(`[ctrl] start err: ${err}`);
      });
    } else if (text === 'stop') {
      console.log('[ctrl] stop');
      exec('bash ~/m4_stop.sh', (err, so, se) => {
        console.log(`[ctrl] stop done: ${(so || se || '').trim()}`);
        if (err) console.log(`[ctrl] stop err: ${err}`);
      });
    }
  });

  ws.on('close', () => console.log(`[conn] -1 (total ${wss.clients.size})`));
});

setInterval(() => {
  // （M7.2：裸 snapshot.ply 的 mtime 监控已由 m7 模块的 delta/full 推送取代）

  // 检测最终结果：tum_mono 已退出 → 发 final + 归档 + 自动除雾 + 状态
  if (pushedOnce && !finalSent) {
    let alive = true;
    // 用 pgrep -x 精确匹配「进程名 == tum_mono」，避免：
    // 1) -f 匹配到自身 sh -c 包装（老 bug）；2) -f 匹配到编译/链接 tum_mono 的
    // g++/make 等无关进程（误判存活，导致上一轮采集永远不触发 finalize）。
    try { execSync('pgrep -x tum_mono', { stdio: 'ignore' }); } catch (_) { alive = false; }
    if (!alive) {
      finalSent = true;
      broadcast('final');                                    // 已完成建模（兼容旧消息）
      broadcastStatus('modeling_done');                        // 已完成建模
      broadcastStatus('dehazing');                             // 除雾中
      exec('bash ~/m6/finalize_case.sh', (err, so, se) => {
        const out = `${(so || '').trim()} ${(se || '').trim()}`.trim();
        console.log(`[finalize] ${out}`);
        const m = out.match(/FINALIZED (\d+)/);
        const failed = out.includes('dehaze:failed') || out.includes('除雾失败');
        broadcastStatus(failed ? 'dehaze_failed' : 'dehaze_done', { case: m ? m[1] : '' });
      });
    }
  }

  // 取景推荐生成完成检测：framing.json 出现 → 广播 framing_done
  for (const ts of Object.keys(framingJobs)) {
    const fp = path.join(CASES_DIR, ts, 'framing', 'framing.json');
    if (fs.existsSync(fp)) {
      delete framingJobs[ts];
      console.log(`[framing] done case ${ts}`);
      broadcastStatus('framing_done', { case: ts });
    } else if (Date.now() - framingJobs[ts].startedAt > 15 * 60 * 1000) {
      delete framingJobs[ts];
      console.log(`[framing] timeout case ${ts}`);
      broadcastStatus('framing_error', { case: ts });
    }
  }
}, 500);

server.listen(PORT, () => {
  console.log(`[ws] HTTP+WS on :${PORT}, snapshot ${SNAPSHOT_PATH}, cases ${CASES_DIR}`);
});
