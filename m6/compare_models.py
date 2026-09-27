#!/usr/bin/env python3
"""M6.0c 补充：原始模型 vs 处理后模型的对比 + 游离高斯普查 + 屏幕空间雾归因（+定向 prune）。

A) 高斯普查：scale 分布尾部 + 低不透明度大高斯计数（两个模型各一次）
B) 同视角对比：训练视角 / 轨道自由视角 / 后退雾视角，各渲染两模型，算指标 + 像素差，
   并存三联图（原始 | 处理 | 差异）便于人眼核对
C) 雾归因：对雾视角按 屏幕投影半径×不透明度 取 top 贡献高斯，打印其 scale/opacity/位置
D) --prune-fog：按"屏幕半径>R 且 opacity<τ"定向 prune（多视角并集），重载后复测 + 门禁

用法：
  LD_PRELOAD=... python compare_models.py --orig <ply> --proc <ply> --cameras <cameras.json> \
      --imgdir <Photo-SLAM image 目录> --outdir /tmp/m6cmp [--prune-fog --fog-r 150 --fog-op 0.3]
"""
import argparse
import json
import math
import os

import numpy as np
from PIL import Image
from plyfile import PlyData

import render_pose
import up_utils
from freeview_audit import img_stats, look_at, orthonormal_basis


def census(model, tag):
    xyz = model.get_xyz.detach().cpu().numpy()
    smax = model.get_scaling.detach().cpu().numpy().max(axis=1)
    op = model.get_opacity.detach().cpu().numpy().reshape(-1)
    print(f"[普查:{tag}] N={len(xyz)}")
    for t in (0.125, 0.25, 0.5, 1.0, 2.0, 5.0):
        print(f"    scale>{t:<6}: {int((smax > t).sum()):>6}   其中 opacity<0.3: "
              f"{int(((smax > t) & (op < 0.3)).sum()):>6}")
    print(f"    opacity<0.3 共 {int((op < 0.3).sum())}；scale 中位 {np.median(smax):.4f} "
          f"P99 {np.percentile(smax, 99):.3f} max {smax.max():.2f}")
    return xyz, smax, op


def screen_radius(xyz, smax, R, C, fy, znear=0.05):
    """近似屏幕投影半径（像素）：r ≈ fy * scale / depth。"""
    R = np.asarray(R, np.float64); C = np.asarray(C, np.float64)
    fwd = R[:, 2]
    rel = xyz - C
    depth = rel @ fwd
    right = R[:, 0]; camY = R[:, 1]
    u = rel @ right; v = rel @ camY
    ok = depth > znear
    r = np.full(len(xyz), -1.0)
    r[ok] = fy * smax[ok] / depth[ok]
    return r, u, v, depth, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", required=True)
    ap.add_argument("--proc", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--imgdir", default=None)
    ap.add_argument("--outdir", default="/tmp/m6cmp")
    ap.add_argument("--prune-fog", action="store_true")
    ap.add_argument("--fog-r", type=float, default=150.0, help="屏幕半径阈值(px)")
    ap.add_argument("--fog-op", type=float, default=0.3, help="不透明度阈值")
    ap.add_argument("--fog-out", default="/root/m6/m6_fog_pruned.ply")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    cams = json.load(open(args.cameras))
    up = up_utils.estimate_up(cams)
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    by_id = {c["id"]: c for c in cams}

    m_orig = render_pose.load_model(args.orig)
    print("=== A) 游离高斯普查 ===")
    xyz_o, smax_o, op_o = census(m_orig, "原始")
    m_proc = render_pose.load_model(args.proc)
    xyz_p, smax_p, op_p = census(m_proc, "处理")

    # ---- 视角集合 ----
    traj = np.array([c["position"] for c in cams], np.float32)
    center = np.median(xyz_o, axis=0)
    robust_r = float(np.percentile(np.linalg.norm(xyz_o - center, axis=1), 99))
    e1, e2 = orthonormal_basis(up)
    views = []
    for cid in (0, 45, 90, 135):
        if cid in by_id:
            c = by_id[cid]
            views.append((f"train{cid}", np.asarray(c["rotation"], np.float32),
                          np.asarray(c["position"], np.float32)))
    for k in range(4):
        az = 2 * math.pi * k / 4
        C = center + 2.5 * robust_r * (math.cos(az) * e1 + math.sin(az) * e2)
        views.append((f"orbit{k}", look_at(C, center, up), C))
    c0 = by_id[cams[0]["id"]]
    R0 = np.asarray(c0["rotation"], np.float64)
    C0 = np.asarray(c0["position"], np.float64)
    views.append(("back", R0.astype(np.float32), (C0 - 0.4 * R0[:, 2]).astype(np.float32)))
    views.append(("fwd", R0.astype(np.float32), (C0 + 0.4 * R0[:, 2]).astype(np.float32)))

    # ---- B) 同视角对比 ----
    print("\n=== B) 同视角对比（原始 vs 处理）===")
    print(f"{'view':>10}{'orig: mean/std/dark/ten':>34}{'proc: mean/std/dark/ten':>34}{'|Δ|均值':>9}")
    imgs = {}
    for name, R, C in views:
        cam = render_pose.build_camera(R, C, fx, fy, w, h)
        io = render_pose.render_pose(m_orig, cam)
        ip = render_pose.render_pose(m_proc, cam)
        so, sp = img_stats(io), img_stats(ip)
        d = float(np.abs(io.astype(np.float64) - ip.astype(np.float64)).mean())
        print(f"{name:>10}{so['mean']:>8.1f}/{so['std']:.0f}/{so['dark%']:.0f}/{so['tenengrad']:.0f}"
              f"{sp['mean']:>12.1f}/{sp['std']:.0f}/{sp['dark%']:.0f}/{sp['tenengrad']:.0f}{d:>9.2f}")
        imgs[name] = (io, ip)
        tri = np.concatenate([io, ip,
                              np.clip(np.abs(io.astype(np.int16) - ip.astype(np.int16)) * 6,
                                      0, 255).astype(np.uint8)], axis=1)
        Image.fromarray(tri).save(os.path.join(args.outdir, f"cmp_{name}.png"))

    # ---- C) 雾归因 ----
    print("\n=== C) 雾归因（处理模型，屏幕半径×不透明度 top 贡献）===")
    fogmask = np.zeros(len(xyz_p), bool)
    for name, R, C in views:
        if not (name.startswith("orbit") or name == "back"):
            continue
        r, u, v, depth, ok = screen_radius(xyz_p, smax_p, R, C, fy)
        contrib = np.where(ok, r * op_p, 0.0)
        top = np.argsort(-contrib)[:8]
        print(f"  [{name}] top8 贡献高斯（screen_r px / opacity / scale / xyz）:")
        for i in top:
            print(f"      r={r[i]:8.1f}px op={op_p[i]:.3f} scale={smax_p[i]:7.3f} "
                  f"xyz={np.round(xyz_p[i], 2)}")
        m = ok & (r > args.fog_r) & (op_p < args.fog_op)
        fogmask |= m
        print(f"      → 命中「r>{args.fog_r:.0f}px 且 op<{args.fog_op}」: {int(m.sum())} 个")
    print(f"  并集 fogmask = {int(fogmask.sum())} 个（占 {100*fogmask.mean():.2f}%）")

    # ---- D) 定向 prune + 复测 ----
    if args.prune_fog and fogmask.any():
        print(f"\n=== D) 定向 prune → {args.fog_out} ===")
        ply = PlyData.read(args.proc)
        ply["vertex"].data = ply["vertex"].data[~fogmask]
        ply.write(args.fog_out)
        print(f"  已写 {args.fog_out}（保留 {int((~fogmask).sum())}）")
        m2 = render_pose.load_model(args.fog_out)
        print(f"{'view':>10}{'before ten/dark':>20}{'after ten/dark':>20}")
        for name, R, C in views:
            cam = render_pose.build_camera(R, C, fx, fy, w, h)
            i2 = render_pose.render_pose(m2, cam)
            s2 = img_stats(i2)
            s0 = img_stats(imgs[name][1])
            print(f"{name:>10}{s0['tenengrad']:>12.0f}/{s0['dark%']:.0f}"
                  f"{s2['tenengrad']:>14.0f}/{s2['dark%']:.0f}")
            Image.fromarray(np.concatenate([imgs[name][1], i2], axis=1)).save(
                os.path.join(args.outdir, f"fogprune_{name}.png"))
        if args.imgdir:
            from validate_render import psnr, ssim, find_ref
            print("  门禁：训练视角 vs Photo-SLAM 渲染")
            for cid in (0, 45, 90, 135):
                ref = find_ref(args.imgdir, cid)
                if not ref or cid not in by_id:
                    continue
                c = by_id[cid]
                cam = render_pose.build_camera(np.asarray(c["rotation"], np.float32),
                                               np.asarray(c["position"], np.float32), fx, fy, w, h)
                i2 = render_pose.render_pose(m2, cam)
                ref_img = np.asarray(Image.open(ref).convert("RGB"))
                if i2.shape != ref_img.shape:
                    continue
                g = lambda x: x.astype(np.float64).mean(axis=2)
                print(f"    id{cid}: PSNR {psnr(i2, ref_img):.2f} / SSIM {ssim(g(i2), g(ref_img)):.4f}")


if __name__ == "__main__":
    main()
