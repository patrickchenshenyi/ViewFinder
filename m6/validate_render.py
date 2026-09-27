#!/usr/bin/env python3
"""M6.0b 校验：用 render_pose 渲染训练位姿，与 Photo-SLAM 的 image/<iter>_<id>.jpg 对比。

用法（云端）：
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python validate_render.py --ply <ply> --cameras <cameras.json> \
      --imgdir <image目录> --ids 0 15 30 --outdir /tmp/m6val
"""
import argparse
import glob
import json
import os
import sys

import numpy as np
from PIL import Image

import render_pose


def ssim(a, b, radius=4):
    def box(x, r):
        h, w = x.shape
        k = 2 * r + 1
        xp = np.pad(x, ((r, r), (r, r)), mode="edge")
        c = np.cumsum(np.cumsum(xp, axis=0), axis=1)
        c = np.pad(c, ((1, 0), (1, 0)))
        return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)

    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = box(a, radius), box(b, radius)
    saa = box(a * a, radius) - ma * ma
    sbb = box(b * b, radius) - mb * mb
    sab = box(a * b, radius) - ma * mb
    return float((((2 * ma * mb + C1) * (2 * sab + C2)) /
                  ((ma ** 2 + mb ** 2 + C1) * (saa + sbb + C2))).mean())


def psnr(a, b):
    mse = ((a.astype(np.float64) - b.astype(np.float64)) ** 2).mean()
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


def find_ref(imgdir, cam_id):
    """在 image/ 目录找 *_<id>.jpg（迭代号任意）。"""
    pats = [os.path.join(imgdir, f"*_{cam_id}.jpg"),
            os.path.join(imgdir, f"*_{cam_id}.png")]
    for p in pats:
        hits = glob.glob(p)
        if hits:
            return hits[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--imgdir", required=True)
    ap.add_argument("--ids", nargs="+", type=int, default=[0, 15, 30, 60])
    ap.add_argument("--outdir", default="/tmp/m6val")
    ap.add_argument("--sh", type=int, default=3)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    model = render_pose.load_model(args.ply, sh_degree=args.sh)

    rows = []
    for cid in args.ids:
        ref = find_ref(args.imgdir, cid)
        if ref is None:
            print(f"id {cid}: 找不到参考图 *_<{cid}>.jpg，跳过")
            continue
        with open(args.cameras) as f:
            cams = json.load(f)
        meta = next(c for c in cams if c["id"] == cid)
        R = np.asarray(meta["rotation"], dtype=np.float32)
        C = np.asarray(meta["position"], dtype=np.float32)
        cam = render_pose.build_camera(R, C, meta["fx"], meta["fy"],
                                       meta["width"], meta["height"])
        mine = render_pose.render_pose(model, cam)
        ref_img = np.asarray(Image.open(ref).convert("RGB"))
        if mine.shape != ref_img.shape:
            print(f"id {cid}: 尺寸不一致 mine={mine.shape} ref={ref_img.shape}")
            continue
        out = os.path.join(args.outdir, f"mine_{cid}.png")
        Image.fromarray(mine).save(out)
        g = lambda x: np.asarray(x, dtype=np.float64).mean(axis=2)
        rows.append((cid, psnr(mine, ref_img), ssim(g(mine), g(ref_img)),
                     os.path.basename(ref), out))

    print(f"\n{'id':>4}{'PSNR':>9}{'SSIM':>9}  参考图")
    for cid, p, s, ref, out in rows:
        print(f"{cid:>4}{p:>9.2f}{s:>9.4f}  {ref}")
    if rows:
        ps = np.array([r[1] for r in rows])
        ss = np.array([r[2] for r in rows])
        print(f"\n平均 PSNR {ps.mean():.2f} dB / SSIM {ss.mean():.4f}  "
              f"（SSIM>0.9 即渲染器与 Photo-SLAM 对齐）")


if __name__ == "__main__":
    main()
