#!/usr/bin/env bash
# ViewFinder 公共环境：加载 .env、配置 SSH 密码认证、封装 ssh/scp。
# 用法：在其它 vf_*.sh 里 `source "$(dirname "${BASH_SOURCE[0]}")/vf_env.sh"`
VF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$VF_ROOT/.env"

# 读 .env（支持 "key = value" 与 "key=value"）
_load() {
  sed -nE "s/^[[:space:]]*${1}[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | head -1 | sed -E 's/[[:space:]]*$//'
}

VF_HOST="$(_load viewfinder_host)"
VF_PORT="$(_load viewfinder_port)"
VF_USER="$(_load viewfinder_username)"
VF_PASS="$(_load viewfinder_password)"
VF_PHONE="$(_load ip_webcam_lan_ip)"

[ -n "$VF_HOST" ] && [ -n "$VF_PORT" ] && [ -n "$VF_USER" ] || {
  echo "✗ .env 缺 viewfinder_host/port/username"; exit 1; }

# SSH 密码认证辅助（SSH_ASKPASS）
cat > /tmp/askpass_vf.sh <<EOF
#!/bin/sh
printf '%s\n' '$VF_PASS'
EOF
chmod +x /tmp/askpass_vf.sh
export DISPLAY=:0 SSH_ASKPASS=/tmp/askpass_vf.sh SSH_ASKPASS_REQUIRE=force

VF_SSHOPT="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -p $VF_PORT $VF_USER@$VF_HOST"
VF_SCPOPT="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -P $VF_PORT"

vf_ssh() { ssh $VF_SSHOPT "$@"; }
vf_scp() { scp $VF_SCPOPT "$@"; }
