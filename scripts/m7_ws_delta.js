// M7.2: 增量推流模块（Phase A 云侧）—— id-indexed 全量同步(0x00) + 增量 delta(0x01)。
//
// 与 m4_ws_server.js 解耦：既可独立跑（测协议），也可被主服务 attach() 集成。
// 文件约定（由 M7.1 C++ 写出，位于 mapper 的 result_dir/deltas/）：
//   <deltasDir>/delta_<iter>.bin  -> 0x01 payload（u32 added + u32 removed + added×(i64+62f32) + removed×i64）
//   <deltasDir>/full_<iter>.bin   -> 0x00 payload（u32 count + count×(i64+62f32)）
// 二进制消息首字节：0x00 = 全量同步，0x01 = 增量。裸 ply（首字节 'p'）留给旧客户端整幅重载。
//
// 关键语义：
//  - 启动即对「已存在的 delta/full」做基线，只推「启动之后新产生的」，避免重启误推旧数据触发 finalize。
//  - 新连接先补发最新全量，再重放「全量之后」的增量，保证重连窗口内的变化不丢。
//  - 每推一个新 delta/full（非补发）调用 onPush()，供主服务驱动「是否发生过采集」的判定。
const fs = require('fs');
const path = require('path');
const http = require('http');
const { WebSocketServer } = require('ws');

function readFile(p) { try { return fs.readFileSync(p); } catch (_) { return null; } }

function iterOf(name) { const m = name.match(/\d+/); return m ? parseInt(m[0], 10) : -1; }

function listBin(dir, prefix) {
  try {
    return fs.readdirSync(dir)
      .filter((f) => f.startsWith(prefix) && f.endsWith('.bin'))
      .sort((a, b) => iterOf(a) - iterOf(b));
  } catch (_) { return []; }
}
function maxIter(files) { return files.reduce((m, f) => Math.max(m, iterOf(f)), -1); }

function makeFull(buf) { const b = Buffer.alloc(1 + buf.length); b[0] = 0x00; buf.copy(b, 1); return b; }
function makeDelta(buf) { const b = Buffer.alloc(1 + buf.length); b[0] = 0x01; buf.copy(b, 1); return b; }

function broadcast(wss, data) {
  let n = 0;
  for (const c of wss.clients) if (c.readyState === 1) { c.send(data); n++; }
  return n;
}

// 附带当前迭代号（文本消息，先于二进制推送，浏览器据此显示「迭代 N」）
function broadcastIter(wss, kind, iter) {
  broadcast(wss, JSON.stringify({ type: 'm7_iter', kind, iter }));
}

function attach({ wss, deltasDir, fullThrottleMs = 15000, onLog = console.log, onPush = () => {}, onReset = () => {} }) {
  // 基线：启动时把已存在的文件记为已处理，之后只推新产生的
  let lastDeltaIter = maxIter(listBin(deltasDir, 'delta_'));
  let latestFullBuf = null;
  let latestFullIter = -1;
  let lastFullIter = -1;
  let lastFullPushAt = 0;

  // m4_start.sh 每次采集会 rm -rf 结果目录，tum_mono 重启后迭代号从 0 重来。
  // 检测「迭代号回退 / 目录被清空」= 新采集开始，重置基线并回调 onReset。
  function detectReset() {
    const dMax = maxIter(listBin(deltasDir, 'delta_'));
    const fMax = maxIter(listBin(deltasDir, 'full_'));
    if (dMax < lastDeltaIter || fMax < latestFullIter) {
      lastDeltaIter = -1;
      latestFullIter = -1;
      latestFullBuf = null;
      lastFullIter = -1;
      lastFullPushAt = 0;
      onLog('[m7] capture reset detected (iteration rollback)');
      onReset();
    }
  }

  function scanFull() {
    const files = listBin(deltasDir, 'full_');
    if (!files.length) return;
    const last = files[files.length - 1];
    const it = iterOf(last);
    if (it > latestFullIter) {
      const buf = readFile(path.join(deltasDir, last));
      if (buf) { latestFullIter = it; latestFullBuf = buf; }
    }
  }
  scanFull();           // 载入已存在的最新全量（供新连接补发）
  lastFullIter = latestFullIter;  // 基线：已存在的全量不算「新全量」

  function pushFullIfDue(force) {
    scanFull();
    if (!latestFullBuf) return;
    if (!force && latestFullIter <= lastFullIter) return;      // 无新全量
    const now = Date.now();
    if (!force && now - lastFullPushAt < fullThrottleMs) return;
    lastFullPushAt = now;
    lastFullIter = latestFullIter;
    broadcastIter(wss, 'full', latestFullIter);
    const n = broadcast(wss, makeFull(latestFullBuf));
    onLog(`[m7] full #${latestFullIter} -> ${n} client(s)`);
    if (n > 0) onPush();
  }

  function scanDeltas() {
    const files = listBin(deltasDir, 'delta_');
    for (const f of files) {
      const it = iterOf(f);
      if (it <= lastDeltaIter) continue;
      const buf = readFile(path.join(deltasDir, f));
      if (!buf) continue;
      lastDeltaIter = it;
      broadcastIter(wss, 'delta', it);
      const n = broadcast(wss, makeDelta(buf));
      onLog(`[m7] delta #${it} -> ${n} client(s)`);
      if (n > 0) onPush();
    }
  }

  // 新连接：先补发最新全量（重置累计表基线），再重放「全量之后」的增量补上重连窗口
  wss.on('connection', (ws) => {
    scanFull();
    if (latestFullBuf) {
      ws.send(JSON.stringify({ type: 'm7_iter', kind: 'full', iter: latestFullIter }));
      ws.send(makeFull(latestFullBuf));
      onLog(`[m7] conn: sent full #${latestFullIter}`);
    }
    const replay = listBin(deltasDir, 'delta_').filter((f) => iterOf(f) > latestFullIter);
    for (const f of replay) {
      const buf = readFile(path.join(deltasDir, f));
      if (buf) {
        ws.send(JSON.stringify({ type: 'm7_iter', kind: 'delta', iter: iterOf(f) }));
        ws.send(makeDelta(buf));
      }
    }
    if (replay.length) onLog(`[m7] conn: replayed ${replay.length} delta(s) after full #${latestFullIter}`);
  });

  const timer = setInterval(() => { detectReset(); scanDeltas(); pushFullIfDue(false); }, 500);
  return {
    stop: () => clearInterval(timer),
    pushFullNow: () => pushFullIfDue(true),
    lastDeltaIter: () => lastDeltaIter,
    lastFullIter: () => lastFullIter,
  };
}

// 独立测试模式：node m7_ws_delta.js <deltasDir> [port]
if (require.main === module) {
  const deltasDir = process.argv[2] || path.join(process.env.HOME, 'Photo-SLAM/results/m5_live/deltas');
  const port = Number(process.argv[3]) || 8766;
  const server = http.createServer((req, res) => { res.writeHead(200); res.end('m7 delta ws'); });
  const wss = new WebSocketServer({ server });
  attach({ wss, deltasDir });
  server.listen(port, () => console.log(`[m7] standalone ws on :${port}, deltasDir ${deltasDir}`));
}

module.exports = { attach };
