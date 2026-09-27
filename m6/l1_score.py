#!/usr/bin/env python3
"""M6.3.1 v2: L1 规则分（修复版）—— 按"跑通全链路"优先级改进。

改动（对应另一 session「四、按跑通全链路排的优先级」6 条）：
  1. 剥支撑面重聚类：RANSAC 主平面 → 残差点云聚类 → 主体变成真实物体（不再"桌面+一半家具"）
  2. 逐候选反推主体：族 A 不再全局借用 subject0，取"投影面积最大且在前方"的主体
  3. 修 third 口径：加"竖直三分线"原型（y 自由）+ 族 B 按自身构图意图打分
  4. contrast 去饱和：批内百分位归一化（消族偏置、不饱和）
  5. top-K 去重（uv 距离 < 阈值剔除）+（主体×原型）配额
  6. 自检：top-5 两两 uv 距离 > 0.05；top-5 至少覆盖 2 个不同主体

用法（云端，无 GPU / 无 LD_PRELOAD）：
  python l1_score.py --candidates /root/m6/candidates.json --ply /root/m6/m6_fog_pruned.ply \
      --cameras <cameras.json> --renderdir /tmp/m6cand3 \
      --out /root/m6/l1_top_v2.json --outdir /tmp/m6l1v2
"""
import argparse
import json
import math
import os
import re

import numpy as np
from PIL import Image
from plyfile import PlyData
from scipy import ndimage


def clamp01(x):
    return float(min(1.0, max(0.0, x)))


# ---------------- 主体聚类（剥支撑面） ----------------
def ransac_plane(xyz, eps=0.02, n_iter=1500, seed=0):
    rng = np.random.default_rng(seed)
    best = (0, None, None)
    for _ in range(n_iter):
        p0, p1, p2 = xyz[rng.choice(len(xyz), 3, replace=False)]
        n = np.cross(p1 - p0, p2 - p0)
        ln = np.linalg.norm(n)
        if ln < 1e-9:
            continue
        n = n / ln
        d = -(n @ p0)
        c = int((np.abs(xyz @ n + d) < eps).sum())
        if c > best[0]:
            best = (c, n, d)
    return best


def cluster_subjects(xyz, voxel=0.05, min_points=100, topk=6):
    n_before = len(xyz)
    # 1) 剥支撑面（桌面/地面）：去掉离主平面很近的点，剩下"凸起物"
    c, n, d = ransac_plane(xyz)
    removed = 0
    if c > 0.08 * n_before:
        dist = xyz @ n + d
        keep = np.abs(dist) > 0.02
        removed = int((~keep).sum())
        xyz = xyz[keep]
        print(f"  [clust] 剥支撑面：主平面内点 {c}（{100*c/n_before:.0f}%），"
              f"去除近平面点 {removed}，剩 {len(xyz)}")

    # 2) 残差聚类（连通域）
    vmin = xyz.min(axis=0)
    shape = tuple(((xyz.max(axis=0) - vmin) / voxel).astype(int) + 1)
    grid = np.zeros(shape, np.uint8)
    idx = ((xyz - vmin) / voxel).astype(int)
    np.add.at(grid, tuple(idx.T), 1)
    thr = max(2, int(grid.max() * 0.02))
    labels, nlab = ndimage.label(grid >= thr)
    subjects = []
    if nlab:
        for lab in range(1, nlab + 1):
            pts = xyz[labels[tuple(idx.T)] == lab]
            if len(pts) < min_points:
                continue
            cc = pts.mean(axis=0)
            rr = float(np.sqrt(((pts - cc) ** 2).sum(axis=1).mean()))
            subjects.append({"center": cc, "radius": rr, "count": len(pts)})
    subjects.sort(key=lambda s: -s["count"])
    return subjects[:topk]


def parse_subject(name):
    m = re.match(r"s(\d+)_cam(\d+)_(\w+)", name)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3)
    return None


# ---------------- 几何投影 + 规则分 ----------------
def project(R, C, X, fx, fy, w, h):
    R = np.asarray(R, np.float64); C = np.asarray(C, np.float64)
    Xc = R.T @ (np.asarray(X, np.float64) - C)
    depth = float(Xc[2])
    if depth <= 1e-6:
        return None
    u = fx * Xc[0] / depth + w / 2.0
    v = fy * Xc[1] / depth + h / 2.0
    return u, v, depth


def pick_subject(subjects, R, C, fx, fy, w, h, si):
    """si 给定 → 用该主体；si=None → 逐候选反推（投影面积最大且在前方）。"""
    if si is not None and 0 <= si < len(subjects):
        return subjects[si], si
    best, best_area, best_i = None, 0.0, 0
    for i, s in enumerate(subjects):
        pr = project(R, C, s["center"], fx, fy, w, h)
        if pr is None:
            continue
        u, v, depth = pr
        r_px = fy * s["radius"] / depth
        area = r_px * r_px
        if area > best_area:
            best_area, best, best_i = area, s, i
    if best is None:
        return subjects[0], 0
    return best, best_i


def third_score(uu, vv, intent):
    if intent == "left3":
        return clamp01(1.0 - abs(uu - 1 / 3) / 0.30)
    if intent == "right3":
        return clamp01(1.0 - abs(uu - 2 / 3) / 0.30)
    if intent == "center":
        return clamp01(1.0 - abs(uu - 0.5) / 0.30)
    thirds = [(1 / 3, 1 / 3), (2 / 3, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 2 / 3)]
    inter = clamp01(1.0 - min(math.hypot(uu - a, vv - b) for a, b in thirds) / 0.30)
    line = clamp01(1.0 - min(abs(uu - 1 / 3), abs(uu - 2 / 3)) / 0.30)  # 竖直三分线，y 自由
    return max(inter, line)


def proto_of(uu, vv, intent):
    if intent:
        return intent
    d_center = math.hypot(uu - 0.5, vv - 0.5)
    d_third = min(math.hypot(uu - a, vv - b) for a, b in
                  [(1 / 3, 1 / 3), (2 / 3, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 2 / 3)])
    return "center" if d_center < d_third else "third"


def rule_scores(R, C, subj, up, fx, fy, w, h, ten, intent=None):
    pr = project(R, C, subj["center"], fx, fy, w, h)
    if pr is None:
        return {"third": 0.0, "size": 0.0, "tilt": 0.0, "headroom": 0.0,
                "contrast": 0.0, "uv": None, "behind": True}
    u, v, depth = pr
    uu, vv = u / w, v / h
    uv = (uu, vv)

    third = third_score(uu, vv, intent)
    diag = math.hypot(w, h)
    size_frac = (fy * subj["radius"] / depth) / diag
    size = clamp01(1.0 - abs(size_frac - 0.2) / 0.25)
    Rr = np.asarray(R, np.float64)
    roll = math.degrees(math.asin(float(np.clip(Rr[:, 0] @ up, -1.0, 1.0))))
    tilt = clamp01(1.0 - abs(roll) / 15.0)
    headroom = clamp01(1.0 - 2.0 * abs(vv - 0.5))
    return {"third": third, "size": size, "tilt": tilt, "headroom": headroom,
            "contrast": clamp01(ten / 60.0), "uv": uv, "behind": False}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--renderdir", default=None)
    ap.add_argument("--out", default="/root/m6/l1_top_v2.json")
    ap.add_argument("--outdir", default="/tmp/m6l1v2")
    ap.add_argument("--w-third", type=float, default=0.40)
    ap.add_argument("--w-size", type=float, default=0.20)
    ap.add_argument("--w-tilt", type=float, default=0.10)
    ap.add_argument("--w-headroom", type=float, default=0.10)
    ap.add_argument("--w-contrast", type=float, default=0.20)
    ap.add_argument("--uv-thresh", type=float, default=0.05)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    W = {"third": args.w_third, "size": args.w_size, "tilt": args.w_tilt,
         "headroom": args.w_headroom, "contrast": args.w_contrast}

    data = json.load(open(args.candidates))
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    up = np.asarray(data["up"], np.float64)

    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    subjects = cluster_subjects(xyz)
    print(f"[l1v2] 主体 {len(subjects)} 个：")
    for i, s in enumerate(subjects):
        print(f"     subject{i}: count={s['count']} center={np.round(s['center'],2)} r={s['radius']:.2f}")

    # 第一遍：算几何 + 收集 ten
    scored = []
    for r in data["results"]:
        R = np.asarray(r["R"], np.float64); C = np.asarray(r["C"], np.float64)
        si = parse_subject(r["name"])
        subj, subj_i = pick_subject(subjects, R, C, fx, fy, w, h, si[0] if si else None)
        intent = si[2] if si else None
        sc = rule_scores(R, C, subj, up, fx, fy, w, h, r["stats"]["tenengrad"], intent)
        sc["name"] = r["name"]; sc["family"] = r["family"]; sc["valid"] = r["valid"]
        sc["subject"] = subj_i; sc["intent"] = intent
        if sc["uv"]:
            sc["proto"] = proto_of(sc["uv"][0], sc["uv"][1], intent)
        else:
            sc["proto"] = intent or "?"
        sc["R"] = r["R"]; sc["C"] = r["C"]; sc["stats"] = r["stats"]
        sc["ten"] = r["stats"]["tenengrad"]
        scored.append(sc)

    # 第二遍：contrast 批内百分位归一化（去饱和、消族偏置）
    tens = np.array([s["ten"] for s in scored])
    ranks = np.argsort(np.argsort(tens)).astype(np.float64)
    for s, c in zip(scored, ranks / max(len(tens) - 1, 1)):
        s["contrast"] = float(c)

    for s in scored:
        s["score"] = sum(W[k] * s[k] for k in W)

    valid = [s for s in scored if s["valid"]]
    invalid = [s for s in scored if not s["valid"]]

    # top-K 选择：去重 + 主体×原型配额
    def select(topk):
        sel, keys = [], set()
        for s in sorted(valid, key=lambda x: -x["score"]):
            if s["uv"] is None:
                continue
            if any(math.hypot(s["uv"][0] - t["uv"][0], s["uv"][1] - t["uv"][1]) < args.uv_thresh
                   for t in sel):
                continue
            k = (s["subject"], s["proto"])
            if k in keys:
                continue
            sel.append(s); keys.add(k)
            if len(sel) >= topk:
                break
        return sel

    top5 = select(5)
    top20 = select(20)
    print(f"\n[l1v2] valid={len(valid)} invalid={len(invalid)}；top-5：")
    print(f"{'name':<20}{'fam':>3}{'subj':>5}{'proto':>9}{'L1':>7}{'third':>7}{'size':>7}{'tilt':>7}{'head':>7}{'contr':>7}")
    for s in top5:
        print(f"{s['name']:<20}{s['family']:>3}{s['subject']:>5}{str(s['proto']):>9}{s['score']:>7.3f}"
              f"{s['third']:>7.3f}{s['size']:>7.3f}{s['tilt']:>7.3f}{s['headroom']:>7.3f}{s['contrast']:>7.3f}")

    # 自检
    all_sorted = sorted(scored, key=lambda x: -x["score"])
    top_invalid_rank = next((i + 1 for i, s in enumerate(all_sorted) if not s["valid"]), None)
    print(f"\n[l1v2] 自检·最高 invalid 全局排名 第 {top_invalid_rank}/{len(all_sorted)}；"
          f"top-20 全 valid: {'PASS ✅' if all(s['valid'] for s in top20) else 'FAIL ❌'}")
    # 去重自检
    uvs = [s["uv"] for s in top5]
    min_sep = min(math.hypot(uvs[i][0] - uvs[j][0], uvs[i][1] - uvs[j][1])
                  for i in range(len(uvs)) for j in range(i + 1, len(uvs))) if len(uvs) > 1 else 1.0
    print(f"[l1v2] 自检·top-5 两两 uv 最小距离 {min_sep:.3f}（>0.05）: "
          f"{'PASS ✅' if min_sep > 0.05 else 'FAIL ❌'}")
    n_subj = len(set(s["subject"] for s in top5))
    print(f"[l1v2] 自检·top-5 覆盖 {n_subj} 个不同主体（≥2）: "
          f"{'PASS ✅' if n_subj >= 2 else 'FAIL ❌'}")
    n_proto = len(set(s["proto"] for s in top5))
    print(f"[l1v2] 自检·top-5 覆盖 {n_proto} 种原型")

    def slim(s):
        return {"name": s["name"], "family": s["family"], "score": s["score"],
                "subject": s["subject"], "proto": s["proto"],
                "comp": {k: s[k] for k in W}, "uv": s["uv"],
                "R": s["R"], "C": s["C"], "stats": s["stats"]}
    json.dump({"weights": W, "up": up.tolist(), "n_subjects": len(subjects),
               "subjects": [{"center": s["center"].tolist(), "radius": s["radius"], "count": s["count"]}
                            for s in subjects],
               "top5": [slim(s) for s in top5], "top20": [slim(s) for s in top20],
               "selfcheck": {"top_invalid_rank": top_invalid_rank,
                             "min_uv_sep": min_sep, "n_subjects_top5": n_subj,
                             "n_protos_top5": n_proto}},
              open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"[l1v2] 已写 {args.out}")

    if args.renderdir and top5:
        cells = []
        for s in top5:
            p = os.path.join(args.renderdir, s["name"] + ".png")
            if os.path.exists(p):
                cells.append(np.asarray(Image.open(p).convert("RGB")))
        if cells:
            cw, ch = 320, 180
            sheet = np.zeros((ch, cw * len(cells), 3), np.uint8)
            for i, im in enumerate(cells):
                sheet[:, i * cw:(i + 1) * cw] = np.asarray(
                    Image.fromarray(im).resize((cw, ch), Image.LANCZOS))
            Image.fromarray(sheet).save(os.path.join(args.outdir, "l1v2_top5.png"))
            print(f"[l1v2] top-5 sheet: {os.path.join(args.outdir, 'l1v2_top5.png')}")


if __name__ == "__main__":
    main()
