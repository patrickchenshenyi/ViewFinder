#!/usr/bin/env python3
"""M6.6.6: 初筛退位 —— 只做 去重 + 配额 + coverage，美学全交 VLM。

不再用美学加权（third/size/tilt/headroom 不参与选择），避免公式化构图偏见：
  ① 分桶（族×方向×距离/意图）+ 桶内 coverage 优先 + 轮询取满 target（保证多样性）；
  ② uv 去重（主导主体投影位置距离 < uv_thresh 视为重复）；
  ③ 主体×原型配额（仅 F-C，同主体同构图只进一条）。

输出：selected（≈target 个），并镜像为 top20 供 vlm_score.py 直接使用。

用法（云端，无需 GPU/LD_PRELOAD）：
  python screen.py --candidates /root/m6/candidates2.json --ply /root/m6/m6_fog_pruned.ply \
      --cameras <cameras.json> --renderdir /tmp/m6cand2 --out /root/m6/screen.json \
      --outdir /tmp/m6screen --target 40
"""
import argparse
import json
import math
import os
import re

import numpy as np
from PIL import Image
from plyfile import PlyData

from l1_score import cluster_subjects, project


def parse_name(name, family):
    if family == "A":
        m = re.match(r"kf\d+_(back|right|left|up)([\d.]+)", name)
        d = float(m.group(2))
        db = "<=1.5" if d <= 1.5 else ("<=3" if d <= 3 else ">3")
        return f"A-{m.group(1)}-{db}", None, None
    if family == "D":
        m = re.match(r"kf\d+_yaw([+-]\d+)", name)
        return f"D-yaw{m.group(1)}", None, None
    if family == "B":
        m = re.match(r"kf\d+_(\w+)", name)
        return f"B-{m.group(1)}", None, None
    if family == "C":
        m = re.match(r"s(\d+)_cam\d+_(\w+)", name)
        return f"C-s{m.group(1)}-{m.group(2)}", int(m.group(1)), m.group(2)
    return f"?-{name}", None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--renderdir", default=None)
    ap.add_argument("--out", default="/root/m6/screen.json")
    ap.add_argument("--outdir", default="/tmp/m6screen")
    ap.add_argument("--target", type=int, default=40)
    ap.add_argument("--uv-thresh", type=float, default=0.06)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    data = json.load(open(args.candidates))
    results = data["results"]
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]

    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    subjects = cluster_subjects(xyz)
    subj0 = subjects[0]
    print(f"[screen] 主体 {len(subjects)} 个（去重用主导主体 subject0）")

    rows = []
    for r in results:
        bucket, si, proto = parse_name(r["name"], r["family"])
        subj = subjects[si] if si is not None and si < len(subjects) else subj0
        pr = project(np.asarray(r["R"], np.float64), np.asarray(r["C"], np.float64),
                     subj["center"], fx, fy, w, h)
        uv = (pr[0] / w, pr[1] / h) if pr else None
        rows.append({"name": r["name"], "family": r["family"], "bucket": bucket,
                     "subject": si, "proto": proto, "uv": uv,
                     "coverage": r["coverage"]["coverage"],
                     "R": r["R"], "C": r["C"], "crops": r["crops"], "stats": r["stats"]})

    buckets = {}
    for r in rows:
        buckets.setdefault(r["bucket"], []).append(r)
    for b in buckets.values():
        b.sort(key=lambda x: -x["coverage"])
    print(f"[screen] 候选 {len(rows)}，分桶 {len(buckets)} 个")

    # 轮询选择：**桶内** uv 去重（跨距离的视角即使主体 uv 相近，环境交代完全不同，不能去重）
    # + 主体×原型配额（F-C）
    selected, seen_uv, quota = [], {}, {}
    bucket_list = sorted(buckets.keys())
    idx = {b: 0 for b in bucket_list}
    while len(selected) < args.target and any(idx[b] < len(buckets[b]) for b in bucket_list):
        progressed = False
        for b in bucket_list:
            buv = seen_uv.setdefault(b, [])
            while idx[b] < len(buckets[b]):
                c = buckets[b][idx[b]]
                idx[b] += 1
                if c["uv"] is None:
                    continue
                if any(math.hypot(c["uv"][0] - u[0], c["uv"][1] - u[1]) < args.uv_thresh
                       for u in buv):
                    continue
                if c["family"] == "C" and quota.get((c["subject"], c["proto"]), 0) >= 1:
                    continue
                selected.append(c)
                buv.append(c["uv"])
                if c["family"] == "C":
                    quota[(c["subject"], c["proto"])] = quota.get((c["subject"], c["proto"]), 0) + 1
                progressed = True
                break
        if not progressed:
            break

    # 自检
    n_buckets = len(set(c["bucket"] for c in selected))
    per_bucket_seps = []
    for b in set(c["bucket"] for c in selected):
        uvs = [c["uv"] for c in selected if c["bucket"] == b]
        for i in range(len(uvs)):
            for j in range(i + 1, len(uvs)):
                per_bucket_seps.append(math.hypot(uvs[i][0] - uvs[j][0], uvs[i][1] - uvs[j][1]))
    min_inbucket = min(per_bucket_seps) if per_bucket_seps else 1.0
    fams = {}
    for c in selected:
        fams[c["family"]] = fams.get(c["family"], 0) + 1
    print(f"\n[screen] 选出 {len(selected)}（target {args.target}）")
    print(f"[screen] 自检·覆盖桶数 {n_buckets}/{len(buckets)}；"
          f"**桶内** uv 最小距离 {min_inbucket:.3f}（>{args.uv_thresh}，跨桶 uv 可接近——距离/环境不同，设计如此）；"
          f"族分布 {fams}")
    print(f"[screen] 自检·美学加权已移除：选择全程只用 coverage/去重/配额，"
          f"纯纹理/倾斜/大留白候选不会被构图规则排除")

    def slim(c):
        return {"name": c["name"], "family": c["family"], "bucket": c["bucket"],
                "coverage": c["coverage"], "uv": c["uv"],
                "subject": c["subject"] if c["subject"] is not None else 0,
                "proto": c["proto"],
                "R": c["R"], "C": c["C"], "crops": c["crops"], "stats": c["stats"]}
    json.dump({"target": args.target, "n_selected": len(selected),
               "selected": [slim(c) for c in selected],
               "top20": [slim(c) for c in selected[:20]]},
              open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"[screen] 已写 {args.out}")

    # contact sheet（供人眼检查多样性）
    if args.renderdir:
        cells = []
        for c in selected:
            p = os.path.join(args.renderdir, c["name"] + ".png")
            if os.path.exists(p):
                cells.append(np.asarray(Image.open(p).convert("RGB")))
        if cells:
            cw, ch, cols = 200, 112, 8
            rows_n = (len(cells) + cols - 1) // cols
            sheet = np.zeros((ch * rows_n, cw * cols, 3), np.uint8)
            for i, im in enumerate(cells):
                r_, col = divmod(i, cols)
                sheet[r_ * ch:(r_ + 1) * ch, col * cw:(col + 1) * cw] = \
                    np.asarray(Image.fromarray(im).resize((cw, ch), Image.LANCZOS))
            Image.fromarray(sheet).save(os.path.join(args.outdir, "screen_sheet.png"))
            print(f"[screen] contact sheet: {os.path.join(args.outdir, 'screen_sheet.png')}"
                  f"（{len(cells)} 张）")


if __name__ == "__main__":
    main()
