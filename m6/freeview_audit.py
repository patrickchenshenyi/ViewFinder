#!/usr/bin/env python3
"""M6.0c: 自由视角质量体检 —— 几何统计 + 轨道采样渲染 + 量化"糊雾"。

用法（云端）：
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python freeview_audit.py --ply <ply> --cameras <cameras.json> \
      --outdir /tmp/m6c [--naz 12] [--orbit 2.5]
"""
import argparse
import json
import math
import os
import sys

import numpy as np
from PIL import Image

import render_pose


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def tenengrad(gray):
    gy, gx = np.gradient(gray.astype(np.float64))
    return float((gx * gx + gy * gy).mean())


def img_stats(img):
    g = np.asarray(Image.fromarray(img).convert("L"), dtype=np.float64)
    return {
        "mean": float(g.mean()),
        "std": float(g.std()),
        "dark%": float(100.0 * (g < 16).mean()),
        "tenengrad": tenengrad(g),
    }


def orthonormal_basis(up):
    up = up / (np.linalg.norm(up) + 1e-9)
    e1 = np.array([1.0, 0.0, 0.0]) if abs(up[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = e1 - up * float(e1 @ up)
    e1 = e1 / (np.linalg.norm(e1) + 1e-9)
    e2 = np.cross(up, e1)
    e2 = e2 / (np.linalg.norm(e2) + 1e-9)
    return e1, e2


def look_at(C, target, up):
    """返回 R_c2w（相机 +Z 前 / +Y 下，COLMAP 约定），相机中心 C 看向 target。"""
    f = np.asarray(target, np.float64) - np.asarray(C, np.float64)
    f = f / (np.linalg.norm(f) + 1e-9)
    right = np.cross(f, up)
    right = right / (np.linalg.norm(right) + 1e-9)
    camY = np.cross(f, right)  # 相机 +Y = 世界"下"
    R = np.stack([right, camY, f], axis=1).astype(np.float32)
    return R


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--outdir", default="/tmp/m6c")
    ap.add_argument("--naz", type=int, default=12)
    ap.add_argument("--orbit", type=float, default=2.5)
    ap.add_argument("--sh", type=int, default=3)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    model = render_pose.load_model(args.ply, sh_degree=args.sh)
    with open(args.cameras) as f:
        cams = json.load(f)

    # ---- 1) 几何统计 ----
    xyz = model.get_xyz.detach().cpu().numpy()          # (N,3)
    scale = model.get_scaling.detach().cpu().numpy()     # (N,3) activated
    opacity = model.get_opacity.detach().cpu().numpy().reshape(-1)  # activated
    N = xyz.shape[0]
    med = np.median(xyz, axis=0)
    lo = np.percentile(xyz, 1, axis=0)
    hi = np.percentile(xyz, 99, axis=0)
    d = np.linalg.norm(xyz - med, axis=1)
    robust_r = float(np.percentile(d, 99))
    smax = scale.max(axis=1)
    print(f"[stats] N={N}")
    print(f"[stats] xyz 中位={np.round(med,3)}  1%={np.round(lo,3)}  99%={np.round(hi,3)}")
    print(f"[stats] 稳健半径(99%)={robust_r:.3f}  全量半径={d.max():.3f}")
    print(f"[stats] scale(激活) 中位={np.median(smax):.3f} "
          f"P90={np.percentile(smax,90):.3f} P99={np.percentile(smax,99):.3f} max={smax.max():.3f}")
    print(f"[stats] opacity 中位={np.median(opacity):.4f} "
          f"%<0.01={100*(opacity<0.01).mean():.1f}%  %<0.1={100*(opacity<0.1).mean():.1f}%")

    # ---- 2) 上方向估计（轨迹均值 up，即 M6.1 方法） ----
    ups = []
    for c in cams:
        R = np.asarray(c["rotation"], np.float32)
        ups.append(-R[:, 1])  # 相机 +Y 是"下"，取负 = 世界上
    up = np.mean(ups, axis=0)
    up = up / (np.linalg.norm(up) + 1e-9)
    print(f"[up] 轨迹均值 up={np.round(up,4)}")

    # ---- 3) 轨道自由视角 + 训练视角参照 ----
    center = med
    traj = np.array([c["position"] for c in cams], np.float32)
    traj_r = float(np.linalg.norm(traj - traj.mean(0), axis=1).max())
    orbit_r = args.orbit * max(robust_r, traj_r, 1e-3)
    e1, e2 = orthonormal_basis(up)
    fx = cams[0]["fx"]; fy = cams[0]["fy"]; w = cams[0]["width"]; h = cams[0]["height"]

    rows = []
    # 训练视角（参照，用真实 id 打标签；自检：打印选中的 id）
    train_idx = [0, len(cams) // 2, len(cams) - 1]
    print(f"[baseline] 训练参照相机 id = {[cams[i]['id'] for i in train_idx]}（按列表下标取头/中/尾）")
    for i in train_idx:
        c = cams[i]
        R = np.asarray(c["rotation"], np.float32); C = np.asarray(c["position"], np.float32)
        img = render_pose.render_pose(model, render_pose.build_camera(R, C, fx, fy, w, h))
        rows.append((f"train_{c['id']}", img))

    # 自由视角（轨道）
    for k in range(args.naz):
        az = 2 * math.pi * k / args.naz
        C = center + orbit_r * (math.cos(az) * e1 + math.sin(az) * e2)
        R = look_at(C, center, up)
        img = render_pose.render_pose(model, render_pose.build_camera(R, C, fx, fy, w, h))
        rows.append((f"free_{k:02d}", img))

    print(f"\n{'view':>12}{'mean':>8}{'std':>8}{'dark%':>8}{'tenengrad':>11}")
    for name, img in rows:
        s = img_stats(img)
        print(f"{name:>12}{s['mean']:>8.1f}{s['std']:>8.1f}{s['dark%']:>8.1f}{s['tenengrad']:>11.1f}")
        Image.fromarray(img).save(os.path.join(args.outdir, f"{name}.png"))

    # ---- 拼 contact sheet ----
    cell = 360
    sheet = np.zeros((cell, cell * len(rows), 3), np.uint8)
    for i, (name, img) in enumerate(rows):
        im = np.asarray(Image.fromarray(img).resize((cell, cell)))
        sheet[:, i * cell:(i + 1) * cell] = im
    Image.fromarray(sheet).save(os.path.join(args.outdir, "contact_sheet.png"))
    print(f"\n[out] contact_sheet.png 与单帧已存到 {args.outdir}")


if __name__ == "__main__":
    main()
