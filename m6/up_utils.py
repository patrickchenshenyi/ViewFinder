#!/usr/bin/env python3
"""M6.1: 上方向估计 + 验收（修正版）。

**估计（俯仰不变）**：pitch 是绕相机右轴的旋转，**不改变右轴**；roll≈0 时所有右轴
共面于水平面，故水平面法向（= 重力 up）= 右轴协方差最小特征值对应的特征向量。
（旧版用 `mean(-R[:,1])`——相机下轴随俯仰变化，会被平均俯仰污染：本数据偏差 32°。）

**验收共 3 项**（旧版只有第 1 项，对俯仰轴错误失明）：
  1. roll：相对估计 up 的 roll mean/std 要小（|μ|<5°、std<10°）
  2. 平面交叉校验：RANSAC 最大平面若为水平面，其法向应与 up 夹角 <20°（可发现轴错误）
  3. 俯仰只做记录（手持向下扫拍属正常，不做通过线）

用法：
  python up_utils.py --cameras <cameras.json> [--ply <ply>]
"""
import argparse
import json
import math

import numpy as np


def estimate_up(cams):
    """右轴零空间法（俯仰不变）。返回单位 up 向量。"""
    R = np.array([c["rotation"] for c in cams], dtype=np.float64)
    right = R[:, :, 0]
    M = right.T @ right
    w, V = np.linalg.eigh(M)
    up = V[:, 0]
    ref = -R[:, :, 1].mean(axis=0)          # 定向参考（避免翻转）
    if up @ ref < 0:
        up = -up
    return up / (np.linalg.norm(up) + 1e-9)


def camera_roll_deg(R, up):
    d = float(np.clip(np.asarray(R, dtype=np.float64)[:, 0] @ up, -1.0, 1.0))
    return math.degrees(math.asin(d))


def camera_pitch_deg(R, up):
    d = float(np.clip(np.asarray(R, dtype=np.float64)[:, 2] @ up, -1.0, 1.0))
    return math.degrees(math.asin(d))


def _stats(v):
    v = np.asarray(v, dtype=np.float64)
    return {"n": len(v), "mean": float(v.mean()), "std": float(v.std()),
            "p5": float(np.percentile(v, 5)), "p95": float(np.percentile(v, 95))}


def roll_stats(cams, up):
    return _stats([camera_roll_deg(c["rotation"], up) for c in cams])


def pitch_stats(cams, up):
    return _stats([camera_pitch_deg(c["rotation"], up) for c in cams])


def plane_cross_check(ply_path, up, eps=0.02, n_iter=2000, seed=0, min_inlier=0.15):
    """RANSAC 最大平面法向与 up 的夹角（水平面→≈0°；墙面→≈90°）。"""
    from plyfile import PlyData
    v = PlyData.read(ply_path)["vertex"]
    P = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    rng = np.random.default_rng(seed)
    if len(P) > 20000:
        P = P[rng.choice(len(P), 20000, replace=False)]
    best = (0, None)
    for _ in range(n_iter):
        p0, p1, p2 = P[rng.choice(len(P), 3, replace=False)]
        n = np.cross(p1 - p0, p2 - p0)
        ln = np.linalg.norm(n)
        if ln < 1e-9:
            continue
        n = n / ln
        c = int((np.abs(P @ n - n @ p0) < eps).sum())
        if c > best[0]:
            best = (c, n)
    if best[1] is None or best[0] < min_inlier * len(P):
        return None
    n = best[1]
    if n @ up < 0:
        n = -n
    a = math.degrees(math.acos(float(np.clip(n @ up, -1.0, 1.0))))
    return {"inlier_ratio": best[0] / len(P), "angle_deg": a, "normal": n.tolist()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--ply", default=None, help="给则做平面交叉校验")
    args = ap.parse_args()

    cams = json.load(open(args.cameras))
    up = estimate_up(cams)
    rs = roll_stats(cams, up)
    ps = pitch_stats(cams, up)
    print(f"[M6.1] up(右轴零空间) = [{up[0]:+.4f}, {up[1]:+.4f}, {up[2]:+.4f}]")
    print(f"[M6.1] 自检1 roll : mean={rs['mean']:+.2f}° std={rs['std']:.2f}° "
          f"p5={rs['p5']:+.2f}° p95={rs['p95']:+.2f}°（n={rs['n']}）"
          f" → {'PASS ✅' if abs(rs['mean']) < 5 and rs['std'] < 10 else 'FAIL ❌'}")
    print(f"[M6.1] 记录 pitch : mean={ps['mean']:+.2f}° std={ps['std']:.2f}°"
          f"（负=整体向下看，手持扫拍正常）")

    ok = abs(rs["mean"]) < 5 and rs["std"] < 10
    if args.ply:
        pc = plane_cross_check(args.ply, up)
        if pc is None:
            print("[M6.1] 自检2 平面交叉校验：无占优平面（场景无地面/斜面）→ 跳过（记为已知局限）")
        else:
            good = pc["angle_deg"] < 20 or abs(pc["angle_deg"] - 90) < 20
            print(f"[M6.1] 自检2 平面交叉校验：最大平面内点 {100*pc['inlier_ratio']:.1f}%，"
                  f"法向与 up 夹角 {pc['angle_deg']:.1f}°（≈0°=水平面 / ≈90°=墙面）"
                  f" → {'PASS ✅' if good else 'FAIL ❌'}")
            ok = ok and good
    print(f"[M6.1] 验收: {'PASS ✅' if ok else 'FAIL ❌'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
