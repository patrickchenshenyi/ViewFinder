#!/usr/bin/env bash
# 一键拉起全链路：反隧道(18080→手机) + 正隧道(8765) + 云 node 服务，并做健康检查。
# 用法：bash vf_up.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/vf_env.sh"

echo "== [1/3] 反隧道 18080 → 手机 $VF_PHONE =="
# 清理上一次残留：反隧道循环脚本 + 真正持有隧道的孤儿 ssh 子进程。
# 只 pkill 脚本名是不够的——旧会话遗留的 `ssh -N -R 18080:...` 会继续占死
# 云上的 18080（且还指向旧手机 IP），新隧道因 ExitOnForwardFailure 永远建不起来。
pkill -f "reverse_tunnel[.]sh" 2>/dev/null || true
pkill -f "vf_fwd_tunnel[.]sh" 2>/dev/null || true
pkill -f "[s]sh -N .*-R 18080:" 2>/dev/null || true
pkill -f "[s]sh -N .*-L 8765:" 2>/dev/null || true
sleep 2
# 关键：还要清掉「云端」残留的 18080 占用者。旧隧道 client 死后，云 sshd 没有
# ClientAliveInterval，会抱住反向转发端口 18080 长达 TCP keepalive 默认 ~2 小时，
# 导致新隧道一直 "remote port forwarding failed"（表现为 HTTP 000）。这里直接杀掉。
vf_ssh 'for pid in $(ss -tlnpH "sport = :18080" 2>/dev/null | grep -oP "pid=\K[0-9]+" | sort -u); do kill "$pid" 2>/dev/null && echo "  清掉云端残留 18080 holder pid=$pid"; done; true' || true
sleep 2
nohup bash "$SCRIPT_DIR/reverse_tunnel.sh" > /tmp/rev_tunnel.log 2>&1 < /dev/null &
disown
sleep 3

echo "== [2/3] 正隧道 Mac:8765 → 云:8765 (自动重连) =="
cat > /tmp/vf_fwd_tunnel.sh <<'EOF'
#!/bin/bash
# Mac → 云 正向隧道：Mac 8765 → 云 8765（浏览器 WS / REST 走这条）。自动重连。
opts="$1"
while true; do
  echo "[fwd] 正向隧道连接 8765 → 云:8765 ..."
  ssh -N -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=15 \
      $opts -L 8765:localhost:8765
  echo "[fwd] 断开，2s 后重连"; sleep 2
done
EOF
chmod +x /tmp/vf_fwd_tunnel.sh
nohup bash /tmp/vf_fwd_tunnel.sh "$VF_SSHOPT" > /tmp/fwd_tunnel.log 2>&1 < /dev/null &
disown
sleep 3
if nc -z -G 2 127.0.0.1 8765 2>/dev/null; then
  echo "  已建立"
else
  echo "  ⚠ 未监听，看 /tmp/fwd_tunnel.log"
fi

echo "== [3/3] 云 node 服务 =="
# 后台起 node（setsid 在云上可用；后台化避免 ssh 偶尔被 node 的 fd 卡住不返回）
vf_ssh 'cd ~/m4_ws && setsid node server.js ~/Photo-SLAM/results/m5_live/live/snapshot.ply 8765 > /tmp/m5_ws.log 2>&1 < /dev/null &' >/dev/null 2>&1 &
sleep 3

echo "== 健康检查 =="
# 隧道建立需要 1–3s；这里最多重试 15s，避免偶发时序导致误报 000
for i in 1 2 3 4 5 6 7; do
  code=$(vf_ssh 'curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:18080/' 2>/dev/null || echo 000)
  [ "$code" = "200" ] && break
  sleep 2
done
echo "  云→手机(18080) HTTP $code"
if nc -z -G 2 127.0.0.1 8765 2>/dev/null; then echo "  Mac:8765 OK"; else echo "  Mac:8765 未监听"; fi
vf_ssh 'if pgrep -f "node serve[r].js" >/dev/null; then echo "  云 node: 运行中"; else echo "  云 node: 未运行"; fi'
vf_ssh 'echo "  cases:"; curl -s -m 6 http://127.0.0.1:8765/cases | head -c 200; echo'

echo "→ 完成后浏览器开 http://127.0.0.1:8099/?live=ws://localhost:8765"
