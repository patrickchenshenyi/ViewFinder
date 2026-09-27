#!/usr/bin/env python3
"""M6.0c/A1: 全关键帧基线统计 → L0 阈值文件。

对 cameras.json 的每个（或每隔 --step 个）关键帧：
  1) 用 render_pose 重渲染；
  2) 计算 mean/std/dark%/Tenengrad（"存活/覆盖"指标，非保真度）；
  3) 若真值帧存在（img_name 指向原图），计算 渲染 vs 真值 的 PSNR/SSIM（保真度，
     预期 ≈ 重建自身 psnr.txt 基线 ~26dB，而非 M6.0b 口径对齐的 45.8dB）。

输出：屏幕分位数表（p10/25/50/75/90）+ --out 写 JSON（分位数 + 建议 L0 阈值）。
自检：打印实际解析的相机 id 范围/数量与缺失真值数（不允许静默丢样本）。

用法（云端）：
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python baseline_stats.py --ply <ply> --cameras <cameras.json> \
      --frames-dir /root/live --out /tmp/m6_l0_thresholds.json [--step 2]
"""
import argparse
import json
import os

import numpy as np
from PIL import Image

import render_pose
from freeview_audit import img_stats


def psnr(a, b):
    mse = ((a.astype(np.float64) - b.astype(np.float64)) ** 2).mean()
    return 99.0 if mse == 0 else 10 * np.log10(255.0 ** 2 / mse)


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


def quantiles(vals):
    return {f"p{q}": float(np.percentile(vals, q)) for q in (10, 25, 50, 75, 90)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--frames-dir", default=None, help="真值帧根目录（img_name 相对它）")
    ap.add_argument("--out", default=None, help="阈值 JSON 输出路径")
    ap.add_argument("--step", type=int, default=1, help="每 N 个关键帧取 1 个")
    ap.add_argument("--sh", type=int, default=3)
    args = ap.parse_args()

    model = render_pose.load_model(args.ply, sh_degree=args.sh)
    with open(args.cameras) as f:
        cams = json.load(f)

    ids = [c["id"] for c in cams]
    print(f"[baseline] 解析到 {len(ids)} 个相机，id 范围 {min(ids)}..{max(ids)}（每 {args.step} 取 1）")

    fx = cams[0]["fx"]; fy = cams[0]["fy"]; w = cams[0]["width"]; h = cams[0]["height"]
    rows = []
    n_truth = 0
    for i in range(0, len(cams), args.step):
        c = cams[i]
        R = np.asarray(c["rotation"], np.float32); C = np.asarray(c["position"], np.float32)
        cam = render_pose.build_camera(R, C, fx, fy, w, h)
        img = render_pose.render_pose(model, cam)
        s = img_stats(img)
        row = {"id": c["id"], **s}
        truth = None
        if args.frames_dir:
            tp = os.path.join(args.frames_dir, c["img_name"])
            if os.path.exists(tp):
                truth = np.asarray(Image.open(tp).convert("RGB").resize((w, h))).astype(np.float64)
                n_truth += 1
        if truth is not None:
            g = lambda x: x.mean(axis=2)
            row["psnr"] = psnr(img, truth)
            row["ssim"] = ssim(g(img), g(truth))
        rows.append(row)

    print(f"[baseline] 已渲染 {len(rows)} 帧，其中 {n_truth} 帧有真值可比")

    def col(k):
        return np.array([r[k] for r in rows if r.get(k) is not None])

    print(f"\n{'指标':>12}" + "".join(f"{q:>9}" for q in ("p10", "p25", "p50", "p75", "p90")))
    for k in ("mean", "std", "dark%", "tenengrad", "psnr", "ssim"):
        v = col(k)
        if len(v):
            q = quantiles(v)
            print(f"{k:>12}" + "".join(f"{q['p' + str(x)]:>9.2f}" for x in (10, 25, 50, 75, 90)))

    if args.out:
        t = col("tenengrad"); d = col("dark%")
        thresholds = {
            "source": {"ply": args.ply, "n_cams": len(ids), "n_rendered": len(rows), "n_truth": n_truth},
            "quantiles": {k: quantiles(col(k)) for k in ("mean", "std", "dark%", "tenengrad", "psnr", "ssim") if len(col(k))},
            # 建议 L0 阈值（"存活"判据；保真度另看 psnr/ssim）：
            "suggested_l0": {
                "ten_min": float(max(5.0, 0.2 * np.percentile(t, 25))),   # 存活下限
                "dark_max": float(min(80.0, 1.5 * np.percentile(d, 75))),  # 黑屏上限
                "fog_rule": {"mean_gt": 85.0, "std_lt": 25.0},            # 白雾判别
                "note": "阈值基于训练关键帧基线；L0 用低分辨率预筛时须重标定；Tenengrad 只当'存活'用",
            },
        }
        with open(args.out, "w") as f:
            json.dump(thresholds, f, indent=2, ensure_ascii=False)
        print(f"\n[baseline] 阈值文件已写 {args.out}")
        if len(col("psnr")):
            print(f"[baseline] 渲染 vs 真值：PSNR p50={np.percentile(col('psnr'),50):.2f}dB "
                  f"（重建质量基线，非渲染器口径）")


if __name__ == "__main__":
    main()
