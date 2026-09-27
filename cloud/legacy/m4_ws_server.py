#!/usr/bin/env python3
# M4.4: 云端 WebSocket 推送服务 —— 监控快照 ply，变化即广播二进制给所有客户端
# 用法：python3 m4_ws_server.py <snapshot.ply路径> [端口]
# 依赖：pip install websockets
import asyncio, os, sys
import websockets

SNAPSHOT_PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/Photo-SLAM/results/live/snapshot.ply"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8765

clients = set()
last_mtime = 0.0
last_size = -1

def read_snapshot():
    if not os.path.exists(SNAPSHOT_PATH):
        return None
    return open(SNAPSHOT_PATH, "rb").read()

async def broadcast(data):
    dead = []
    for ws in list(clients):
        try:
            await ws.send(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
    return len(clients) - len(dead)

async def watch_loop():
    global last_mtime, last_size
    while True:
        try:
            if os.path.exists(SNAPSHOT_PATH):
                mtime = os.path.getmtime(SNAPSHOT_PATH)
                size = os.path.getsize(SNAPSHOT_PATH)
                if (mtime != last_mtime or size != last_size) and size > 0:
                    last_mtime, last_size = mtime, size
                    data = read_snapshot()
                    n = await broadcast(data)
                    print(f"[watch] snapshot {size} bytes -> {n} client(s)", flush=True)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[watch] err: {e}", flush=True)
        await asyncio.sleep(0.5)

async def handler(ws):
    clients.add(ws)
    print(f"[conn] +1 (total {len(clients)})", flush=True)
    try:
        data = read_snapshot()
        if data:
            await ws.send(data)
        await ws.wait_closed()
    finally:
        clients.discard(ws)
        print(f"[conn] -1 (total {len(clients)})", flush=True)

async def main():
    print(f"[ws] watching {SNAPSHOT_PATH} on :{PORT}", flush=True)
    async with websockets.serve(handler, "0.0.0.0", PORT):
        await watch_loop()

asyncio.run(main())
