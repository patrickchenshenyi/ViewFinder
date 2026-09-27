#!/usr/bin/env python3
"""M6.0c 补充：游离高斯（floater）检测 + 清理。

检测（三判据，OR 合并成 floater mask）：
  F1 超大尺度: max(scale) > max(scale_k * P99(smax), scale_frac * robust_r)
              （分位数稳健版；旧版 0.1*robust_r 绝对分支会把 >25cm 的墙面/地面全误伤）
  F2 位置离群: dist > max(dist_k * robust_r, P99.9(dist))
              （MAD z-score 在此紧凑分布上失效——6×MAD≈0.3m 会把 10% 的高斯误判，已弃用）
  F3 低不透明度且大: opacity < op_lo 且 max(scale) > op_scale_frac * s_thresh
  TODO(F4): 视图一致性（只被 ≤1 个训练视角可见）——floater 的"多视角可解释性"定义，比调阈值治本
  TODO: 雾归因（对雾视角按 屏幕投影尺寸×opacity 取 top-N 贡献高斯做定向 prune）

处理：
  1) 默认只打印统计（--out 不给则不改任何文件）；
  2) --out 给则写过滤后的 ply（原地保留原文件）；
  3) 复测建议：对 pruned ply 重跑 validate_render.py（训练 SSIM 不降太多）
     与 nearview_audit.py（雾视角 Tenengrad 是否改善）。

用法：
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python prune_floaters.py --ply <ply> [--out <pruned.ply>]
"""
import argparse

import numpy as np
from plyfile import PlyData


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--scale_k", type=float, default=10.0, help="F1: P99(smax) 的倍数")
    ap.add_argument("--scale_frac", type=float, default=0.5, help="F1: robust_r 的比例（硬上限分支）")
    ap.add_argument("--dist_k", type=float, default=3.0, help="F2: robust_r 的倍数（与 P99.9 取 max）")
    ap.add_argument("--op_lo", type=float, default=0.05, help="F3: 低不透明度阈值")
    ap.add_argument("--op_scale_frac", type=float, default=0.5, help="F3: s_thresh 的比例")
    args = ap.parse_args()

    ply = PlyData.read(args.ply)
    v = ply["vertex"]
    N = len(v.data)
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    scale_raw = np.stack([v[f"scale_{i}"] for i in range(3)], axis=1).astype(np.float64)
    scale = np.exp(scale_raw)                      # 激活：exp(log-scale)
    op = sigmoid(v["opacity"].astype(np.float64))  # 激活：sigmoid(logit-opacity)
    smax = scale.max(axis=1)

    med = np.median(xyz, axis=0)
    dist = np.linalg.norm(xyz - med, axis=1)
    robust_r = float(np.percentile(dist, 99))
    s_p99 = float(np.percentile(smax, 99))
    s_branch_quant = args.scale_k * s_p99
    s_branch_abs = args.scale_frac * robust_r
    s_thresh = max(s_branch_quant, s_branch_abs)
    mad = float(np.median(np.abs(dist - np.median(dist))))
    d_p999 = float(np.percentile(dist, 99.9))
    d_thresh = max(args.dist_k * robust_r, d_p999)

    F1 = smax > s_thresh
    F2 = dist > d_thresh
    F3 = (op < args.op_lo) & (smax > args.op_scale_frac * s_thresh)
    floater = F1 | F2 | F3

    dominant = "P99分支" if s_branch_quant >= s_branch_abs else "robust_r分支"
    print(f"[floater] N={N}  robust_r={robust_r:.3f}  s_thresh={s_thresh:.4f}"
          f"（P99分支={s_branch_quant:.4f} vs robust_r分支={s_branch_abs:.4f}，主导={dominant}）")
    print(f"[floater] F1 超大尺度: {int(F1.sum())}  (max_scale={smax.max():.3f})")
    if F1.sum():
        fs = smax[F1]
        print(f"[floater]   F1 命中 scale 分位: p25={np.percentile(fs, 25):.3f} "
              f"p50={np.percentile(fs, 50):.3f} p90={np.percentile(fs, 90):.3f}")
    print(f"[floater] F2 位置离群: {int(F2.sum())}  (d_thresh={d_thresh:.3f}"
          f"=max({args.dist_k}xrobust_r={args.dist_k * robust_r:.3f}, P99.9={d_p999:.3f})，"
          f"MAD参考={mad:.3f}, max_dist={dist.max():.3f})")
    print(f"[floater] F3 低op+大 : {int(F3.sum())}")
    print(f"[floater] 合并(OR)   : {int(floater.sum())}  ({100*floater.mean():.2f}%)")

    if args.out:
        kept = ~floater
        ply["vertex"].data = ply["vertex"].data[kept]
        ply.write(args.out)
        print(f"[floater] 已写 {args.out}（保留 {int(kept.sum())}/{N} 高斯）")


if __name__ == "__main__":
    main()
