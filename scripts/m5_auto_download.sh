#!/bin/bash
# M5.2: 自动下载云端最终 ply 到本地 results/<run>/
# 用法：bash scripts/m5_auto_download.sh <run_name>   （默认 m5_live）
# 依赖：ViewFinder/.env 里的 viewfinder_* 连接信息
set -uo pipefail   # 不用 -e，容忍 ssh 瞬断

RUN="${1:-m5_live}"
REMOTE="~/Photo-SLAM/results/${RUN}"
LOCAL="results/${RUN}_$(date +%Y%m%d_%H%M%S)"

cd "$(dirname "$0")/.."   # 切到项目根
[ -f .env ] || { echo "找不到 .env"; exit 1; }
HOST=$(grep '^viewfinder_host' .env | cut -d= -f2 | tr -d ' ')
PORT=$(grep '^viewfinder_port' .env | cut -d= -f2 | tr -d ' ')
USER=$(grep '^viewfinder_username' .env | cut -d= -f2 | tr -d ' ')
PASS=$(grep '^viewfinder_password' .env | cut -d= -f2 | tr -d ' ')

cat > /tmp/askpass_vf.sh <<EOF
#!/bin/sh
printf '%s\n' '$PASS'
EOF
chmod +x /tmp/askpass_vf.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/askpass_vf.sh SSH_ASKPASS_REQUIRE=force
SSHOPT="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -p $PORT $USER@$HOST"

echo "[m5.2] 轮询云端最终 ply: $REMOTE"
while true; do
  PLY=$(ssh $SSHOPT "find $REMOTE -name 'point_cloud.ply' 2>/dev/null | grep shutdown | head -1" 2>/dev/null || true)
  if [ -n "$PLY" ]; then
    S1=$(ssh $SSHOPT "stat -c %s '$PLY' 2>/dev/null" 2>/dev/null || echo 0)
    sleep 6
    S2=$(ssh $SSHOPT "stat -c %s '$PLY' 2>/dev/null" 2>/dev/null || echo 0)
    if [ "$S1" = "$S2" ] && [ "$S1" != "0" ] && [ -n "$S1" ]; then
      mkdir -p "$LOCAL"
      echo "[m5.2] 就绪，下载中（$S1 字节）"
      ssh $SSHOPT "cat '$PLY'" > "$LOCAL/point_cloud.ply"
      echo "[m5.2] 完成: $LOCAL/point_cloud.ply（$(wc -c < "$LOCAL/point_cloud.ply" | tr -d ' ') 字节）"
      break
    fi
  fi
  sleep 10
done
