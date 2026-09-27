#!/usr/bin/env python3
"""M6.3.2: PyIQA 学习分（NIMA 美学）融合 L1 排序。

在 M6.3.1 规则分基础上，加 NIMA 美学分（AVA 训练），加权融合后重排：
  fused = w_rule * rule + w_learned * nima_norm   （nima 批内 min-max 归一化到 [0,1]）

只对 valid 候选算 NIMA（invalid 已被 L0 硬过滤、无渲染图）。
自检：① top-20 全 valid；② rule 与 nima 的相关性（冗余还是互补）；③ 融合后 top 变化。

用法（云端）：
  HF_ENDPOINT=https://hf-mirror.com python l1_pyiqa.py \
      --candidates /root/m6/candidates.json --ply /root/m6/m6_fog_pruned.ply \
      --cameras <cameras.json> --renderdir /tmp/m6cand3 \
      --out /root/m6/l1_fused.json --outdir /tmp/m6l1f
"""
import argparse
import json
import os

import numpy as np
import pyiqa

from l1_score import cluster_subjects, rule_scores, parse_subject


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--renderdir", required=True)
    ap.add_argument("--out", default="/root/m6/l1_fused.json")
    ap.add_argument("--outdir", default="/tmp/m6l1f")
    ap.add_argument("--metric", default="nima", choices=["nima", "musiq", "clipiqa", "laion_aes"])
    ap.add_argument("--device", default="0")
    ap.add_argument("--w-rule", type=float, default=0.5)
    ap.add_argument("--w-learned", type=float, default=0.5)
    ap.add_argument("--w-third", type=float, default=0.35)
    ap.add_argument("--w-size", type=float, default=0.20)
    ap.add_argument("--w-tilt", type=float, default=0.10)
    ap.add_argument("--w-headroom", type=float, default=0.10)
    ap.add_argument("--w-contrast", type=float, default=0.25)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    W = {"third": args.w_third, "size": args.w_size, "tilt": args.w_tilt,
         "headroom": args.w_headroom, "contrast": args.w_contrast}

    data = json.load(open(args.candidates))
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    up = np.asarray(data["up"], np.float64)

    from plyfile import PlyData
    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    subjects = cluster_subjects(xyz)
    print(f"[l1-f] 主体 {len(subjects)} 个")

    # ---- 规则分（valid + invalid 都算）----
    scored = []
    for r in data["results"]:
        R = np.asarray(r["R"], np.float64); C = np.asarray(r["C"], np.float64)
        si = parse_subject(r["name"])
        subj = subjects[si[0]] if si and si[0] < len(subjects) else subjects[0]
        sc = rule_scores(R, C, subj, up, fx, fy, w, h, r["stats"]["tenengrad"])
        sc["score"] = sum(W[k] * sc[k] for k in W)
        sc["name"] = r["name"]; sc["family"] = r["family"]; sc["valid"] = r["valid"]
        sc["R"] = r["R"]; sc["C"] = r["C"]; sc["stats"] = r["stats"]
        scored.append(sc)

    valid = [s for s in scored if s["valid"]]
    invalid = [s for s in scored if not s["valid"]]

    # ---- NIMA 学习分（只对 valid）----
    device = int(args.device) if args.device.isdigit() else args.device
    iqa = pyiqa.create_metric(args.metric, device=device)
    paths = []
    missing = []
    for s in valid:
        p = os.path.join(args.renderdir, s["name"] + ".png")
        if os.path.exists(p):
            paths.append(p)
        else:
            missing.append(s["name"])
    print(f"[l1-f] valid {len(valid)}，有渲染图 {len(paths)}（缺 {len(missing)}：{missing[:5]}）")

    import torch
    from PIL import Image
    imgs = []
    for p in paths:
        im = Image.open(p).convert("RGB").resize((224, 224))
        imgs.append(np.asarray(im, dtype=np.float32) / 255.0)
    batch = torch.from_numpy(np.stack(imgs)).permute(0, 3, 1, 2).to(device)  # (N,3,224,224)
    scores = np.asarray(iqa(batch).detach().cpu().numpy()).reshape(-1)
    lo, hi = scores.min(), scores.max()
    nima_norm = (scores - lo) / (hi - lo + 1e-9)
    print(f"[l1-f] {args.metric} 原始范围 [{lo:.2f}, {hi:.2f}]（已 min-max 归一化）")

    # ---- 融合 ----
    path_idx = 0
    for s in valid:
        if s["name"] in missing:
            s["nima_norm"] = 0.0
            s["fused"] = args.w_rule * s["score"]
        else:
            s["nima_norm"] = float(nima_norm[path_idx]); path_idx += 1
            s["fused"] = args.w_rule * s["score"] + args.w_learned * s["nima_norm"]

    rule_scores_arr = np.array([s["score"] for s in valid])
    nima_arr = np.array([s["nima_norm"] for s in valid])
    corr = float(np.corrcoef(rule_scores_arr, nima_arr)[0, 1])
    print(f"[l1-f] rule 与 {args.metric} 相关性 = {corr:+.3f}"
          f"（接近 1 = 冗余；接近 0/负 = 互补）")

    valid.sort(key=lambda s: -s["fused"])
    top5 = valid[:5]; top20 = valid[:20]
    print("\n[l1-f] 融合 Top-5：")
    print(f"{'name':<20}{'fam':>3}{'fused':>7}{'rule':>7}{'nima':>7}")
    for s in top5:
        print(f"{s['name']:<20}{s['family']:>3}{s['fused']:>7.3f}{s['score']:>7.3f}"
              f"{s['nima_norm']:>7.3f}")

    # 自检：与规则-only 的 top-20 对比
    rule_top20 = sorted(valid, key=lambda s: -s["score"])[:20]
    overlap = len(set(s["name"] for s in top20) & set(s["name"] for s in rule_top20))
    print(f"[l1-f] 自检·融合 top-20 与规则 top-20 重叠 {overlap}/20")
    imax = invalid[0]["score"] if invalid else 0.0
    all_sorted = sorted(scored, key=lambda s: -(s.get("fused", s["score"])))
    print(f"[l1-f] 自检·top-20 全 valid: {'PASS ✅' if all(s['valid'] for s in top20) else 'FAIL ❌'}")

    def slim(s):
        return {"name": s["name"], "family": s["family"], "fused": s["fused"],
                "rule": s["score"], "nima_norm": s["nima_norm"],
                "R": s["R"], "C": s["C"], "stats": s["stats"]}
    json.dump({"metric": args.metric, "w_rule": args.w_rule, "w_learned": args.w_learned,
               "corr": corr, "nima_range": [float(lo), float(hi)],
               "top5": [slim(s) for s in top5], "top20": [slim(s) for s in top20],
               "rule_top20_overlap": overlap},
              open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"[l1-f] 已写 {args.out}")


if __name__ == "__main__":
    main()
