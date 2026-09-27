#!/usr/bin/env node
/*
 * Viewfinder 3DGS 本地查看器 — 零依赖静态服务器
 *
 * 只暴露 splat-web/dist 目录，避免把项目里的 .env / 训练输出一并发布到本地端口上。
 * /__beacon 接收前端的诊断事件并追加到 splat-web/.viewer-events.log，
 * 这样即使没有图形界面（无头 Chrome）也能确认页面是否真的渲染出了东西。
 */
import { createServer } from 'node:http';
import { createReadStream, promises as fs } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, 'splat-web', 'dist');
const LOG_FILE = path.join(HERE, 'splat-web', '.viewer-events.log');
const PORT = Number(process.env.PORT || 8099);
const HOST = '127.0.0.1';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.ply': 'application/octet-stream',
  '.splat': 'application/octet-stream',
  '.ksplat': 'application/octet-stream',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.wasm': 'application/wasm',
};

function resolveWithinRoot(pathname) {
  const decoded = decodeURIComponent(pathname);
  const target = path.normalize(path.join(ROOT, decoded));
  if (target !== ROOT && !target.startsWith(ROOT + path.sep)) return null;
  return target;
}

async function handleBeacon(url) {
  const params = Object.fromEntries(url.searchParams.entries());
  const line = `${new Date().toISOString()} ${JSON.stringify(params)}\n`;
  try {
    await fs.appendFile(LOG_FILE, line);
  } catch (_) { /* 日志写不了也不影响页面 */ }
  const { event, ...rest } = params;
  console.log(`[beacon] ${event || '?'} ${JSON.stringify(rest)}`);
  return [204, 'text/plain', ''];
}

async function handleStatic(pathname) {
  let filePath = resolveWithinRoot(pathname);
  if (!filePath) return [403, 'text/plain; charset=utf-8', 'forbidden'];

  let stat = await fs.stat(filePath).catch(() => null);
  if (stat && stat.isDirectory()) {
    filePath = path.join(filePath, 'index.html');
    stat = await fs.stat(filePath).catch(() => null);
  }
  if (!stat || !stat.isFile()) return [404, 'text/plain; charset=utf-8', 'not found'];

  return ['file', filePath, stat.size];
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || HOST}`);
  try {
    if (url.pathname === '/__beacon') {
      const [code, type, body] = await handleBeacon(url);
      res.writeHead(code, { 'Content-Type': type, 'Cache-Control': 'no-store' });
      res.end(body);
      return;
    }
    if (url.pathname === '/__health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, root: ROOT, pid: process.pid }));
      return;
    }

    const result = await handleStatic(url.pathname);
    if (req.method === 'HEAD') {
      // 前端探测文件是否存在（模型切换的"未完成"状态），只回状态不传 body
      if (result[0] === 'file') {
        const [, filePath, size] = result;
        res.writeHead(200, {
          'Content-Type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
          'Content-Length': size,
          'Cache-Control': 'no-cache',
        });
      } else {
        const [code, type] = result;
        res.writeHead(code, { 'Content-Type': type });
      }
      res.end();
      return;
    }
    if (result[0] === 'file') {
      const [, filePath, size] = result;
      res.writeHead(200, {
        'Content-Type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
        'Content-Length': size,
        'Cache-Control': 'no-cache',
      });
      createReadStream(filePath).pipe(res);
      return;
    }
    const [code, type, body] = result;
    res.writeHead(code, { 'Content-Type': type });
    res.end(body);
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end(`server error: ${err.message}`);
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[viewfinder] 3DGS viewer: http://${HOST}:${PORT}/`);
  console.log(`[viewfinder] serving ${ROOT}`);
  console.log(`[viewfinder] events -> ${LOG_FILE}`);
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    console.log(`\n[viewfinder] ${sig} received, shutting down`);
    server.close(() => process.exit(0));
  });
}
