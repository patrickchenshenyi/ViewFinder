#!/usr/bin/env python3
"""M6.2: 候选视角生成 + L0 有效性过滤。

族 A · 近轨迹扰动：分层关键帧 × {右/左/上/前移 ±0.4m、偏航 ±15°、俯仰 ±8°}
族 B · 主体锚定（已重建侧）：密度聚类主体 × 能看到它的训练相机 × {居中/左三分/右三分}
L0：tenengrad ≥ ten_min、dark% ≤ dark_max、白雾(mean>85 且 std<25) 剔除

用法（云端）：
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python candidate_gen.py --ply <ply> --cameras <cameras.json> \
      --thresholds /root/m6/l0_thresholds.json \
      --outdir /tmp/m6cand --out /tmp/m6cand.json
"""
import argparse
import json
import math
import os

import numpy as np
from PIL import Image
from scipy import ndimage

import render_pose
import up_utils
from freeview_audit import img_stats, look_at


def compose_R(right, fwd):
    """R_c2w = [right | camY(下) | fwd]，三轴正交归一。"""
    right = right / (np.linalg.norm(right) + 1e-9)
    fwd = fwd / (np.linalg.norm(fwd) + 1e-9)
    camY = np.cross(fwd, right)
    return np.stack([right, camY, fwd], axis=1).astype(np.float32)


def rot_around(v, axis, th):
    axis = np.asarray(axis, np.float64)
    axis = axis / (np.linalg.norm(axis) + 1e-9)
    v = np.asarray(v, np.float64)
    return (v * math.cos(th) + np.cross(axis, v) * math.sin(th)
            + axis * (axis @ v) * (1 - math.cos(th)))


def cluster_subjects(xyz, voxel=0.05, min_points=200, topk=5):
    """体素密度聚类 → 主体列表 [{center, radius, count}]。"""
    vmin = xyz.min(axis=0)
    shape = tuple(((xyz.max(axis=0) - vmin) / voxel).astype(int) + 1)
    grid = np.zeros(shape, np.uint8)
    idx = ((xyz - vmin) / voxel).astype(int)
    np.add.at(grid, tuple(idx.T), 1)

    thr = max(2, int(grid.max() * 0.02))
    labels, n = ndimage.label(grid >= thr)
    if n == 0:
        return []
    subjects = []
    for lab in range(1, n + 1):
        pts = xyz[labels[tuple(idx.T)] == lab]
        if len(pts) < min_points:
            continue
        c = pts.mean(axis=0)
        r = float(np.sqrt(((pts - c) ** 2).sum(axis=1).mean()))
        subjects.append({"center": c, "radius": r, "count": len(pts)})
    subjects.sort(key=lambda s: -s["count"])
    return subjects[:topk]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--thresholds", default=None, help="l0_thresholds.json")
    ap.add_argument("--outdir", default="/tmp/m6cand")
    ap.add_argument("--out", default="/tmp/m6cand.json")
    ap.add_argument("--sh", type=int, default=3)
    ap.add_argument("--famA-stride", type=int, default=10, help="族 A 每 N 个关键帧取 1")
    ap.add_argument("--famB-topk", type=int, default=5, help="族 B 主体数")
    ap.add_argument("--famB-cams", type=int, default=3, help="族 B 每主体相机数")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    # L0 判据（M6.6.2 起）：coverage 可判读性（双背景渲染防"场景含品红/绿内容误判空"）；
    # 旧 ten/dark/fog 仅作元数据保留
    from coverage import render_magenta, render_green, coverage_metrics
    print("[L0] 判据：coverage 可判读性（coverage≥5% 且 structure≥3，双背景，见 m6/coverage.py）")

    model = render_pose.load_model(args.ply, sh_degree=args.sh)
    with open(args.cameras) as f:
        cams = json.load(f)
    ids = [c["id"] for c in cams]
    print(f"[M6.2] {len(ids)} 相机（id {min(ids)}..{max(ids)}）；每 {args.famA_stride} 取 1 做族 A")

    up = up_utils.estimate_up(cams)
    xyz = model.get_xyz.detach().cpu().numpy()
    scene_center = np.median(xyz, axis=0)
    fx = cams[0]["fx"]; fy = cams[0]["fy"]; w = cams[0]["width"]; h = cams[0]["height"]

    # ---- 主体聚类 ----
    subjects = cluster_subjects(xyz, topk=args.famB_topk)
    print(f"[M6.2] 主体聚类：{len(subjects)} 个")
    for i, s in enumerate(subjects):
        print(f"        subject{i}: count={s['count']} center={np.round(s['center'],2)} radius={s['radius']:.2f}")

    candidates = []  # {name, R, C, family}

    def add(name, R, C, family):
        candidates.append({"name": name, "R": np.asarray(R, np.float32),
                           "C": np.asarray(C, np.float32), "family": family})

    # ---- 族 A：近轨迹扰动 ----
    for i in range(0, len(cams), args.famA_stride):
        c = cams[i]
        R = np.asarray(c["rotation"], np.float64)
        C = np.asarray(c["position"], np.float64)
        fwd = R[:, 2]; right = R[:, 0]
        tag = f"kf{c['id']}"
        add(f"{tag}_orig", R, C, "A")
        add(f"{tag}_right", R, C + 0.4 * right, "A")
        add(f"{tag}_left", R, C - 0.4 * right, "A")
        add(f"{tag}_up", R, C + 0.4 * up, "A")
        add(f"{tag}_fwd", R, C + 0.4 * fwd, "A")
        for s, sg in ((1, "yawp"), (-1, "yawn")):
            nf = rot_around(fwd, up, s * math.radians(15))
            nr = rot_around(right, up, s * math.radians(15))
            add(f"{tag}_{sg}", compose_R(nr, nf), C, "A")
        for s, sg in ((1, "pitchup"), (-1, "pitchdn")):
            nf = rot_around(fwd, right, s * math.radians(8))
            add(f"{tag}_{sg}", compose_R(right, nf), C, "A")

    # ---- 族 B：主体锚定（已重建侧） ----
    for si, subj in enumerate(subjects):
        scored = []
        for c in cams:
            R = np.asarray(c["rotation"], np.float64)
            C = np.asarray(c["position"], np.float64)
            dvec = subj["center"] - C
            dist = np.linalg.norm(dvec)
            if dist < 0.3 or dist > 6.0:
                continue
            cosang = float(dvec @ R[:, 2] / (dist + 1e-9))
            if cosang > 0.7:  # 视线夹角 < 45° 才算"能看到"
                scored.append((cosang / max(dist, 1e-3), c))
        scored.sort(key=lambda x: -x[0])
        picked = scored[:args.famB_cams]
        print(f"        subject{si} 可见相机 {len(scored)} 个，取 {len(picked)} 个")
        for rank, (_, c) in enumerate(picked):
            R = np.asarray(c["rotation"], np.float64)
            C = np.asarray(c["position"], np.float64)
            dvec = subj["center"] - C
            dist = np.linalg.norm(dvec)
            cam_right = compose_R(R[:, 0], R[:, 2])[:, 0]
            off = 0.24 * dist  # ≈ 三分点偏移（水平 FOV 71°：tan(35.5°)/3 ≈ 0.24）
            for comp, target in (
                ("center", subj["center"]),
                ("left3", subj["center"] + off * cam_right),
                ("right3", subj["center"] - off * cam_right),
            ):
                Rl = look_at(C, target, up)
                add(f"s{si}_cam{rank}_{comp}", Rl, C, "B")

    print(f"[M6.2] 候选总数 {len(candidates)}（族A/B）")

    # ---- 渲染 + L0（M6.6.2：品红背景渲染算 coverage，只否决完全不可判读）----
    results = []
    valid_imgs = []
    for k, cand in enumerate(candidates):
        cam = render_pose.build_camera(cand["R"], cand["C"], fx, fy, w, h)
        img = render_pose.render_pose(model, cam)                       # 黑背景：展示/打分用
        img_m = render_magenta(model, cand["R"], cand["C"], fx, fy, w, h)  # 品红背景：coverage 用
        img_g = render_green(model, cand["R"], cand["C"], fx, fy, w, h)    # 绿背景：双背景防误判
        cm = coverage_metrics(img_m, img_g)
        s = img_stats(img)
        valid = cm["judgeable"]
        results.append({
            "name": cand["name"], "family": cand["family"], "valid": bool(valid),
            "R": cand["R"].tolist(), "C": cand["C"].tolist(), "stats": s,
            "coverage": cm,
        })
        if valid:
            valid_imgs.append((cand["name"], img, cand["family"]))
        if (k + 1) % 50 == 0:
            print(f"[L0] {k + 1}/{len(candidates)} 已渲染…")

    n_valid = sum(r["valid"] for r in results)
    ratio = n_valid / max(len(results), 1)
    fam = {}
    for r in results:
        fam.setdefault(r["family"], [0, 0])
        fam[r["family"]][1] += 1
        fam[r["family"]][0] += r["valid"]
    print(f"[L0] 有效 {n_valid}/{len(results)} = {100 * ratio:.0f}%"
          + "".join(f"（族{f}: {a}/{b}）" for f, (a, b) in sorted(fam.items())))

    # 覆盖检查：每个主体是否至少有 1 个有效族 B 候选
    for si in range(len(subjects)):
        nv = sum(1 for r in results if r["valid"] and r["name"].startswith(f"s{si}_"))
        print(f"[L0] subject{si} 有效族B候选 {nv}")

    # 输出 JSON + 渲染（全部有效候选分别保存；contact sheet 按族各取前 24）
    with open(args.out, "w") as f:
        json.dump({"n_total": len(results), "n_valid": n_valid, "ratio": ratio,
                   "up": up.tolist(), "subjects": len(subjects),
                   "thresholds": {"ten_min": ten_min, "dark_max": dark_max, "fog": fog},
                   "results": results}, f, indent=1, ensure_ascii=False)
    for name, img, _ in valid_imgs:
        Image.fromarray(img).save(os.path.join(args.outdir, f"{name}.png"))

    def contact_sheet(items, path):
        cw, ch = 320, 180  # 16:9，勿压成正方形（旧版 256x256 会把渲染图拉变形）
        cols = min(len(items), 12)
        sheet = np.zeros((ch, cw * cols, 3), np.uint8)
        for i, (name, img) in enumerate(items[:cols]):
            im = np.asarray(Image.fromarray(img).resize((cw, ch), Image.LANCZOS))
            sheet[:, i * cw:(i + 1) * cw] = im
        Image.fromarray(sheet).save(path)
        print(f"[M6.2] {os.path.basename(path)}: {cols} 张（{cw}x{ch} 每格）")

    for fam, fname in (("A", "contact_sheet_A.png"), ("B", "contact_sheet_B.png")):
        items = [(n, im) for n, im, ff in valid_imgs if ff == fam]
        contact_sheet(items, os.path.join(args.outdir, fname))
    print(f"[M6.2] 完成：{args.out}；有效渲染 {len(valid_imgs)} 张已存 {args.outdir}")
    print(f"[M6.2] 验收：有效占比 {100 * ratio:.0f}%"
          f"{' PASS ✅' if ratio > 0.6 else ' FAIL ❌'}（>60% 通过）")


if __name__ == "__main__":
    main()
