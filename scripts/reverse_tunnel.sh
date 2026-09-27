#!/bin/bash
# Mac → 云 反向隧道：云 18080 → 手机 RTSP（供云端 ffmpeg 拉流）。自动重连 + 自愈。
# 手机 IP 从 .env 读（ip_webcam_lan_ip），改 IP 只需改 .env 后重启本脚本。
#
# 自愈逻辑：旧隧道 client 死后，云 sshd 没有 ClientAliveInterval，会抱住反向端口 18080
# 长达 TCP keepalive 默认 ~2 小时，导致重连一直 "remote port forwarding failed"。
# 这里在连接失败时主动 ssh 到云端，杀掉持有 18080 的残留 sshd，再重试。
cd "$(dirname "$0")/.."
HOST=$(grep '^viewfinder_host' .env | cut -d= -f2 | tr -d ' ')
PORT=$(grep '^viewfinder_port' .env | cut -d= -f2 | tr -d ' ')
USER=$(grep '^viewfinder_username' .env | cut -d= -f2 | tr -d ' ')
PASS=$(grep '^viewfinder_password' .env | cut -d= -f2 | tr -d ' ')
PHONE=$(grep '^ip_webcam_lan_ip' .env | cut -d= -f2 | tr -d ' ')
cat > /tmp/askpass_vf.sh <<EOF
#!/bin/sh
printf '%s\n' '$PASS'
EOF
chmod +x /tmp/askpass_vf.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/askpass_vf.sh SSH_ASKPASS_REQUIRE=force

SSHBASE="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -p $PORT $USER@$HOST"

# 杀掉云端持有 18080 的残留进程（通常是旧 sshd 的 forked 连接）
clear_stale() {
  echo "[rev] 清除云端残留 18080 占用 ..."
  ssh $SSHBASE \
    'for pid in $(ss -tlnpH "sport = :18080" 2>/dev/null | grep -oP "pid=\K[0-9]+" | sort -u); do kill "$pid" 2>/dev/null && echo "cleared pid=$pid"; done; true' 2>/dev/null
}

while true; do
  echo "[rev] 反隧道连接 18080 → $PHONE ..."
  ssh -N $SSHBASE \
      -o ExitOnForwardFailure=yes \
      -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
      -R 18080:$PHONE 2> /tmp/rev_attempt.log
  if grep -q "remote port forwarding failed" /tmp/rev_attempt.log 2>/dev/null; then
    clear_stale
    sleep 2
  fi
  echo "[rev] 断开，2s 后重连"; sleep 2
done
