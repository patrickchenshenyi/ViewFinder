#!/usr/bin/env python3
"""重建质量体检：把某次 Photo-SLAM / 3DGS 运行结果与真值照片逐关键帧对比。

用法：
  python tools/audit_recon_quality.py <run_dir> <seq_dir>

  run_dir: 含 image/1081_<id>.jpg、ply/cameras.json、psnr.txt 的输出目录
           例：results/phone_720p_smoke/1081_shutdown
  seq_dir: 原始帧目录（真值），例：sample_videos/seq

输出（用于回答"精度瓶颈在哪、提高分辨率有没有用"）：
  1. 逐关键帧 PSNR / SSIM / MAE
  2. 渲染锐度 ÷ 真值锐度  —— 远小于 1 说明模型没还原出输入已有的细节（瓶颈在模型端）
  3. 真值 vs 渲染的功率谱频段能量 —— 量化细节损失发生在哪个频段
  4. 误差 vs 离画面中心半径 —— 若误差随半径系统性增大，提示内参/畸变未标定
"""
import json
import os
import re
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_smoke"))
from spectrum import radial_spectrum, band_energy, tenengrad, gray  # noqa: E402


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
    saa, sbb = box(a * a, radius) - ma * ma, box(b * b, radius) - mb * mb
    sab = box(a * b, radius) - ma * mb
    return float((((2 * ma * mb + C1) * (2 * sab + C2)) /
                  ((ma ** 2 + mb ** 2 + C1) * (saa + sbb + C2))).mean())


def main(run_dir, seq_dir):
    cams_path = os.path.join(run_dir, "ply", "cameras.json")
    with open(cams_path) as f:
        cams = json.load(f)
    img_dir = os.path.join(run_dir, "image")
    renders = {}
    for fn in os.listdir(img_dir):
        m = re.match(r"\d+_(\d+)\.(jpg|png)$", fn)
        if m:
            renders[int(m.group(1))] = os.path.join(img_dir, fn)

    rows = []
    for cam in cams:
        k = cam["id"]
        gt_path = os.path.join(seq_dir, cam["img_name"])
        if k not in renders or not os.path.exists(gt_path):
            continue
        a = np.asarray(Image.open(renders[k]).convert("L")).astype(np.float64)
        b = np.asarray(Image.open(gt_path).convert("L")).astype(np.float64)
        if a.shape != b.shape:
            continue
        mse = ((a - b) ** 2).mean()
        psnr = 10 * np.log10(255.0 ** 2 / mse) if mse > 0 else 99.0
        h, w = a.shape
        yy, xx = np.mgrid[0:h, 0:w]
        r = np.sqrt(((yy - h / 2) / (h / 2)) ** 2 + ((xx - w / 2) / (w / 2)) ** 2) / np.sqrt(2)
        d = np.abs(a - b)
        rows.append({
            "id": k,
            "psnr": psnr,
            "ssim": ssim(a, b),
            "mae": float(d.mean()),
            "gt_sharp": tenengrad(b),
            "rd_sharp": tenengrad(a),
            "center": float(d[r < 0.35].mean()),
            "outer": float(d[r > 0.8].mean()),
            "gt_path": gt_path,
            "render_path": renders[k],
        })

    if not rows:
        print("没有可配对的关键帧，检查 run_dir/seq_dir 与 cameras.json 的 img_name")
        return

    print(f"配对成功 {len(rows)} 个关键帧（渲染 = {os.path.basename(renders[min(renders)])} 形式）\n")
    print(f"{'id':>5}{'PSNR':>8}{'SSIM':>8}{'MAE':>8}{'真值锐度':>10}{'渲染锐度':>10}{'锐度比':>8}{'外圈/中心':>10}")
    for r in sorted(rows, key=lambda x: x["id"]):
        print(f"{r['id']:>5}{r['psnr']:>8.2f}{r['ssim']:>8.4f}{r['mae']:>8.2f}"
              f"{r['gt_sharp']:>10.1f}{r['rd_sharp']:>10.1f}"
              f"{r['rd_sharp'] / r['gt_sharp']:>8.2f}{r['outer'] / max(r['center'], 1e-6):>10.2f}")

    psnr = np.array([r["psnr"] for r in rows])
    ratio = np.array([r["rd_sharp"] / r["gt_sharp"] for r in rows])
    radial = np.array([r["outer"] / max(r["center"], 1e-6) for r in rows])
    print(f"\n汇总：PSNR 均值 {psnr.mean():.2f} dB（min {psnr.min():.2f} / max {psnr.max():.2f}）")
    print(f"      渲染/真值锐度比 均值 {ratio.mean():.2f}  ← 明显<1 表示模型没还原输入已有的细节")
    print(f"      外圈/中心误差比 均值 {radial.mean():.2f}（跨帧标准差 {radial.std():.2f}）"
          f"  ← 稳定>1.5 才提示径向畸变未建模")

    # 频谱对比（取锐度中位的那个关键帧，避免极端帧干扰）
    rows_sorted = sorted(rows, key=lambda x: x["gt_sharp"])
    mid = rows_sorted[len(rows_sorted) // 2]
    print(f"\n频段能量占比（中位帧 id={mid['id']}）—— 看细节损失在哪个频段：")
    print(f"{'频段(cycles/px)':<18}{'真值%':>9}{'渲染%':>9}")
    f1, p1 = radial_spectrum(gray(mid["gt_path"]))
    f2, p2 = radial_spectrum(gray(mid["render_path"]))
    b1, b2 = band_energy(f1, p1), band_energy(f2, p2)
    for k in b1:
        print(f"{k:<18}{b1[k]:>9.2f}{b2[k]:>9.2f}")

    print("\n判读：")
    print("  · 锐度比远小于 1 且各频段渲染能量都低于真值 → 瓶颈在模型容量/训练量，提高输入分辨率无用")
    print("  · 锐度比已接近 1（>0.8）→ 模型已用满当前输入，此时提高分辨率才可能有收益")
    print("  · 外圈/中心稳定 >1.5 → 先标定相机内参与畸变，再谈分辨率")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
