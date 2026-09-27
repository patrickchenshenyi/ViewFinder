#!/bin/bash
# 对指定 case 跑 M6.6 取景推荐流水线（两段式渲染提速版）：
#   candidate_gen2 --scale 0.5 粗渲 → screen 去重/配额选桶代表
#   → rerender_selected 仅对桶代表全分辨率重渲染 → vlm_score（VLM 选 top-3）。
# 输出：~/Photo-SLAM/results/cases/<ts>/framing/{framing.json, <name>.png}
#    framing.json = {"top3":[{name, reason, R, C, uv, proto, subject, family}]}
# 用法：bash framing_for_case.sh <case_ts>
set -euo pipefail

TS="${1:-}"
[ -n "$TS" ] || { echo "[framing] 用法: framing_for_case.sh <case_ts>"; exit 1; }

export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6
PY=/root/miniconda3/envs/myconda/bin/python
M6=/root/m6
CASE="$HOME/Photo-SLAM/results/cases/$TS"
PLY="$CASE/dehazed/dehazed.ply"
CAM="$CASE/original/cameras.json"
OUT="$CASE/framing"
WORK="/tmp/framing_$TS"

# 读 API key
KEY=""
if [ -f "$M6/.env" ]; then
  KEY=$(grep -E '^DEEPSEEK_KEY[[:space:]]*=' "$M6/.env" | head -1 | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]*$//')
fi
[ -n "$KEY" ] || { echo "[framing] 缺 DEEPSEEK_KEY（~/m6/.env）"; exit 1; }

[ -f "$PLY" ] || { echo "[framing] 缺除雾模型 $PLY"; exit 1; }
[ -f "$CAM" ] || { echo "[framing] 缺 cameras $CAM"; exit 1; }

rm -rf "$WORK"; mkdir -p "$WORK/proxy" "$WORK/final" "$WORK/screen" "$WORK/vlm" "$OUT"
echo "[framing] case=$TS  ply=$PLY"

cd "$M6"

echo "[framing] 步骤1/4 candidate_gen2 --scale 0.5（粗渲生成候选，采样减半）…"
$PY candidate_gen2.py --ply "$PLY" --cameras "$CAM" \
  --outdir "$WORK/proxy" --out "$WORK/candidates.json" --scale 0.5

echo "[framing] 步骤2/4 screen（去重 + 配额 + coverage，美学交 VLM）…"
$PY screen.py --candidates "$WORK/candidates.json" --ply "$PLY" --cameras "$CAM" \
  --renderdir "$WORK/proxy" --out "$WORK/screen.json" --outdir "$WORK/screen" --target 40

echo "[framing] 步骤3/4 rerender_selected（仅桶代表全分辨率重渲染）…"
$PY rerender_selected.py --ply "$PLY" --cameras "$CAM" \
  --candidates "$WORK/candidates.json" --selection "$WORK/screen.json" \
  --outdir "$WORK/final"

echo "[framing] 步骤4/4 vlm_score（两段式：全员分批入围 → 锦标赛决出 top-3，看全分辨率图）…"
$PY vlm_score.py --l1 "$WORK/screen.json" --renderdir "$WORK/final" \
  --out "$WORK/vlm_top3.json" --outdir "$WORK/vlm" \
  --batch 8 --batch-top 2 --stage2-top 3 --key "$KEY"

# 汇总成网页格式：framing.json（只保留 top3）+ 拷贝渲染图
$PY - "$WORK/vlm_top3.json" "$WORK/final" "$OUT" <<'PY'
import json, os, shutil, sys
vlm_path, renderdir, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(vlm_path))
top3 = d.get("top3", [])
if not top3:
    raise SystemExit("[framing] VLM 未选出 top-3")
for t in top3:
    src = os.path.join(renderdir, t["name"] + ".png")
    if os.path.exists(src):
        shutil.copy(src, os.path.join(outdir, t["name"] + ".png"))
    else:
        print(f"[framing] 警告：缺渲染图 {t['name']}.png")
json.dump({"top3": top3}, open(os.path.join(outdir, "framing.json"), "w"),
          indent=1, ensure_ascii=False)
print(f"[framing] 已写 {outdir}/framing.json（top-3 条）")
PY

echo "FRAMING_DONE $TS"
