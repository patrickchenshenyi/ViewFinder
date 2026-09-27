#!/usr/bin/env python3
"""M6.6.5: 候选生成 v2 —— 扩大范围（M6.6.4 有效域）+ 裁切变体 + 加量 ≥500。

候选族（合计 ≈537）：
  F-A 远距离平移（平移后**重新瞄准场景中心**，模拟真实摄影师后退/横移再取景）：
      12 关键帧 × {back 1–5m(7) + right/left 0.5–5m(12) + up 0.5–2m(4)} = 276
  F-D 偏航（原地）：12 × ±30/±45/±60° = 72
  F-B 近轨迹扰动（旧族 A）：18 × 8 = 144
  F-C 主体锚定（旧族 B）：5 主体 × 3 相机 × 3 构图 = 45

每候选：黑/白双背景渲染（黑兼作展示，白兜底深色内容误判）→
  coverage = 与黑或白背景差异显著像素占比 →
  裁切变体（full / crop_43 / crop_11 / crop_34，按目标覆盖率 ~0.75 取内容质心）→
  L0 = coverage 可判读性（M6.6.2）。

用法（云端，需 LD_PRELOAD）：
  LD_PRELOAD=... python candidate_gen2.py --ply <ply> --cameras <cameras.json> \
      --outdir /tmp/m6cand2 --out /root/m6/candidates2.json
"""
import argparse
import json
import math
import os

import numpy as np
from PIL import Image

import render_pose
import up_utils
from l1_score import cluster_subjects
from coverage import render_white, BLACK, WHITE, coverage_metrics, coverage_mask
from freeview_audit import img_stats, look_at
from plyfile import PlyData


def rot_around(v, axis, th):
    axis = np.asarray(axis, np.float64); axis = axis / (np.linalg.norm(axis) + 1e-9)
    v = np.asarray(v, np.float64)
    return v * math.cos(th) + np.cross(axis, v) * math.sin(th) + axis * (axis @ v) * (1 - math.cos(th))


def compose_R(right, fwd):
    right = right / (np.linalg.norm(right) + 1e-9)
    fwd = fwd / (np.linalg.norm(fwd) + 1e-9)
    return np.stack([right, np.cross(fwd, right), fwd], axis=1)


def crop_variants(mask, w, h, target_cov=0.75):
    """按内容包围盒 + 目标覆盖率生成裁切变体（full + 3 宽高比 4:3/1:1/3:4）。

    用「内容像素数 / 目标覆盖率」反推裁剪面积，使裁后 coverage ≈ target_cov
    （抬高低覆盖率候选 + 保留边缘，不像紧包围盒那样切到 ~1.0 而丢边缘）；
    再确保框内**装下全部内容**（不切内容、只增不减），必要时按宽高比外扩。
    """
    crops = {"full": [0, 0, w, h]}
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return crops
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    bw, bh = x1 - x0, y1 - y0
    content_px = len(ys)
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    for name, ar in (("crop_43", 4 / 3), ("crop_11", 1.0), ("crop_34", 3 / 4)):
        area = content_px / target_cov
        cw = max(int(math.sqrt(area * ar)), 1)
        ch = max(int(cw / ar), 1)
        # 确保装下内容包围盒（不切内容）
        cw = max(cw, bw)
        ch = max(ch, bh)
        # 再平衡到目标宽高比（仍只增不减）
        if cw / ch > ar:
            ch = max(int(round(cw / ar)), bh)
        else:
            cw = max(int(round(ch * ar)), bw)
        crops[name] = [max(0, cx - cw // 2), max(0, cy - ch // 2),
                       min(w, cx - cw // 2 + cw), min(h, cy - ch // 2 + ch)]
    return crops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--outdir", default="/tmp/m6cand2")
    ap.add_argument("--out", default="/root/m6/candidates2.json")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="渲染分辨率倍率（0.5=粗渲，用于第一遍筛选；1.0=全分辨率）")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    model = render_pose.load_model(args.ply)
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w0, h0 = cams[0]["width"], cams[0]["height"]
    # 粗渲倍率：等比例缩放内参与画幅（渲染更快，coverage/裁切框在缩放坐标系下计算）
    w, h = int(w0 * args.scale), int(h0 * args.scale)
    fx, fy = fx * args.scale, fy * args.scale
    up = up_utils.estimate_up(cams)

    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    center = np.median(xyz, axis=0)
    subjects = cluster_subjects(xyz)

    candidates = []

    def add(name, R, C, family):
        candidates.append({"name": name, "R": np.asarray(R, np.float32),
                           "C": np.asarray(C, np.float32), "family": family})

    # ---- F-A 远距离平移（re-aim 场景中心）----
    # 采样减半：每 30 帧取 1 个关键帧（原 15），桶内成员减半，桶数不变（族×方向×距离不变）
    picked = cams[::30]
    print(f"[gen2] F-A 关键帧 {len(picked)} 个（每 30 取 1，采样减半）")
    for c in picked:
        R = np.asarray(c["rotation"], np.float64)
        C = np.asarray(c["position"], np.float64)
        fwd, right = R[:, 2], R[:, 0]
        tag = f"kf{c['id']}"
        for d in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
            Cd = C - d * fwd
            add(f"{tag}_back{d}", look_at(Cd, center, up), Cd, "A")
        for d in (0.5, 1.0, 2.0, 3.0, 4.0, 5.0):
            Cd = C + d * right
            add(f"{tag}_right{d}", look_at(Cd, center, up), Cd, "A")
            Cd = C - d * right
            add(f"{tag}_left{d}", look_at(Cd, center, up), Cd, "A")
        for d in (0.5, 1.0, 1.5, 2.0):
            Cd = C + d * up
            add(f"{tag}_up{d}", look_at(Cd, center, up), Cd, "A")

    # ---- F-D 偏航（原地）----
    for c in picked:
        R = np.asarray(c["rotation"], np.float64)
        C = np.asarray(c["position"], np.float64)
        tag = f"kf{c['id']}"
        for deg in (30.0, 45.0, 60.0):
            for s in (+1.0, -1.0):
                nf = rot_around(R[:, 2], up, s * math.radians(deg))
                nr = rot_around(R[:, 0], up, s * math.radians(deg))
                add(f"{tag}_yaw{s*deg:+.0f}", compose_R(nr, nf), C, "D")

    # ---- F-B 近轨迹扰动（旧族 A）----
    # 采样减半：每 20 帧取 1 个关键帧（原 10），桶内成员减半，桶数不变（族×扰动方向不变）
    picked_b = cams[::20]
    for c in picked_b:
        R = np.asarray(c["rotation"], np.float64)
        C = np.asarray(c["position"], np.float64)
        fwd, right = R[:, 2], R[:, 0]
        tag = f"kf{c['id']}"
        add(f"{tag}_orig", R, C, "B")
        add(f"{tag}_right", R, C + 0.4 * right, "B")
        add(f"{tag}_left", R, C - 0.4 * right, "B")
        add(f"{tag}_up", R, C + 0.4 * up, "B")
        add(f"{tag}_fwd", R, C + 0.4 * fwd, "B")
        for s, sg in ((1, "yawp"), (-1, "yawn")):
            nf = rot_around(fwd, up, s * math.radians(15))
            nr = rot_around(right, up, s * math.radians(15))
            add(f"{tag}_{sg}", compose_R(nr, nf), C, "B")
        for s, sg in ((1, "pitchup"), (-1, "pitchdn")):
            nf = rot_around(fwd, right, s * math.radians(8))
            add(f"{tag}_{sg}", compose_R(right, nf), C, "B")

    # ---- F-C 主体锚定（旧族 B）----
    for si, subj in enumerate(subjects[:5]):
        scored = []
        for c in cams:
            R = np.asarray(c["rotation"], np.float64)
            C = np.asarray(c["position"], np.float64)
            dvec = subj["center"] - C
            dist = np.linalg.norm(dvec)
            if dist < 0.3 or dist > 6.0:
                continue
            cosang = float(dvec @ R[:, 2] / (dist + 1e-9))
            if cosang > 0.7:
                scored.append((cosang / max(dist, 1e-3), c))
        scored.sort(key=lambda x: -x[0])
        for rank, (_, c) in enumerate(scored[:3]):
            R = np.asarray(c["rotation"], np.float64)
            C = np.asarray(c["position"], np.float64)
            dvec = subj["center"] - C
            dist = np.linalg.norm(dvec)
            cam_right = compose_R(R[:, 0], R[:, 2])[:, 0]
            off = 0.24 * dist
            for comp, target in (("center", subj["center"]),
                                 ("left3", subj["center"] + off * cam_right),
                                 ("right3", subj["center"] - off * cam_right)):
                add(f"s{si}_cam{rank}_{comp}", look_at(C, target, up), C, "C")

    print(f"[gen2] 候选总数 {len(candidates)}（A/D/B/C），渲染倍率 {args.scale} → {w}x{h}")

    # ---- 渲染 + coverage + 裁切（黑/白双背景，黑兼作展示）----
    results = []
    n_judge = 0
    for k, cand in enumerate(candidates):
        cam = render_pose.build_camera(cand["R"], cand["C"], fx, fy, w, h)
        img_b = render_pose.render_pose(model, cam)                    # 黑（展示 + coverage 之一）
        img_w = render_white(model, cand["R"], cand["C"], fx, fy, w, h)  # 白（coverage 之二）
        cm = coverage_metrics(img_b, img_w, bg_a=BLACK, bg_b=WHITE)
        mask = coverage_mask(img_b, img_w, bg_a=BLACK, bg_b=WHITE)
        crops = crop_variants(mask, w, h)
        crops_cov = {name: float(mask[b[1]:b[3], b[0]:b[2]].mean())
                     for name, b in crops.items()}
        # 展示图选「内容更可见」的背景：黑覆盖率 ≥ 白覆盖率用黑，否则用白
        cov_b = float(coverage_mask(img_b, None, bg_a=BLACK).mean())
        cov_w = float(coverage_mask(img_w, None, bg_a=WHITE).mean())
        disp = img_b if cov_b >= cov_w else img_w
        s = img_stats(disp)
        valid = cm["judgeable"]
        n_judge += valid
        results.append({
            "name": cand["name"], "family": cand["family"], "valid": bool(valid),
            "R": cand["R"].tolist(), "C": cand["C"].tolist(), "stats": s,
            "coverage": cm, "crops": crops, "crops_cov": crops_cov,
            "disp_bg": "black" if cov_b >= cov_w else "white",
        })
        if valid:
            Image.fromarray(disp).save(os.path.join(args.outdir, f"{cand['name']}.png"))
            for cn, b in crops.items():
                if cn == "full":
                    continue
                Image.fromarray(disp[b[1]:b[3], b[0]:b[2]]).save(
                    os.path.join(args.outdir, f"{cand['name']}_{cn}.png"))
        if (k + 1) % 100 == 0:
            print(f"[gen2] {k + 1}/{len(candidates)} 已渲染（可判读 {n_judge}）")

    ratio = n_judge / len(candidates)
    print(f"\n[gen2] 可判读 {n_judge}/{len(candidates)} = {100*ratio:.0f}%")
    # 抽查：远退候选的裁切框 coverage vs 全图
    print("[gen2] 抽查（远退候选，crop 覆盖率 vs 全图）：")
    for name in ("kf0_back5.0", "kf0_back3.0", "kf0_back1.5"):
        r = next((x for x in results if x["name"] == name), None)
        if r:
            print(f"  {name}: 全图 {r['coverage']['coverage']:.2f} → "
                  + "  ".join(f"{cn} {cv:.2f}" for cn, cv in r["crops_cov"].items()))

    json.dump({"n_total": len(candidates), "n_valid": n_judge, "ratio": ratio,
               "scale": args.scale, "render_w": w, "render_h": h,
               "up": up.tolist(), "center": center.tolist(), "results": results},
              open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"[gen2] 已写 {args.out}")


if __name__ == "__main__":
    main()
