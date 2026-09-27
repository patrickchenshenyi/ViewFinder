#!/usr/bin/env bash
# case 管理：
#   list                      列出已完成 case
#   discard <时间戳>           丢弃某个 case
#   archive <云上结果目录>     把某个结果目录归档为 case（跑除雾）
#   framing <时间戳>           触发该 case 的取景推荐生成（后台，约 10–15 分钟）
# 用法：bash vf_case.sh <list|discard <ts>|archive <dir>|framing <ts>>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/vf_env.sh"

case "${1:-}" in
  list)
    vf_ssh 'curl -s -m 6 http://127.0.0.1:8765/cases | /root/miniconda3/bin/python -m json.tool 2>/dev/null || curl -s -m 6 http://127.0.0.1:8765/cases; echo'
    ;;
  discard)
    TS="${2:-}"; [ -n "$TS" ] || { echo "用法: vf_case.sh discard <时间戳>"; exit 1; }
    echo "→ 丢弃 case $TS"
    vf_ssh "rm -rf ~/Photo-SLAM/results/cases/$TS && echo '已删除 $TS'"
    ;;
  archive)
    DIR="${2:-}"; [ -n "$DIR" ] || { echo "用法: vf_case.sh archive <云上结果目录，如 ~/Photo-SLAM/results/local_xxx>"; exit 1; }
    echo "→ 归档 $DIR（跑除雾）"
    vf_ssh "bash ~/m6/finalize_case.sh $DIR"
    ;;
  framing)
    TS="${2:-}"; [ -n "$TS" ] || { echo "用法: vf_case.sh framing <时间戳>"; exit 1; }
    echo "→ 触发 case $TS 取景推荐生成（后台，约 10–15 分钟；已生成则返回 done）"
    vf_ssh "curl -s -m 6 -X POST http://127.0.0.1:8765/framing?case=$TS; echo"
    ;;
  *)
    echo "用法: vf_case.sh <list|discard <ts>|archive <dir>|framing <ts>>"; exit 1 ;;
esac
