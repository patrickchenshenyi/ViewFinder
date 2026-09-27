#!/usr/bin/env python3
"""诊断 2：裁决 A(相机up均值) 与 B(右轴零空间) 哪个才是真正的重力方向。

两项独立证据：
  ① look_at 重建：族 B 用 up 构造"看向主体"的位姿，若 up 错，则相对真实重力会有 roll。
     分别用 A / B 构造，测相对 B 的 roll。
  ② RANSAC 平面：地面法向 = 重力 up；墙面法向 ⊥ 重力。看最大平面与 A/B 的夹角。
"""
import argparse
import json
import math

import numpy as np


def ang(a, b):
    return math.degrees(math.acos(float(np.clip(a @ b, -1.0, 1.0))))


def look_at_R(C, target, up):
    f = np.asarray(target, float) - np.asarray(C, float)
    f = f / (np.linalg.norm(f) + 1e-9)
    r = np.cross(f, up); r = r / (np.linalg.norm(r) + 1e-9)
    camY = np.cross(f, r)
    return np.stack([r, camY, f], axis=1)


def ransac_planes(P, n_iter=3000, eps=0.02, seed=0):
    rng = np.random.default_rng(seed)
    best = []
    for _ in range(n_iter):
        idx = rng.choice(len(P), 3, replace=False)
        p0, p1, p2 = P[idx]
        n = np.cross(p1 - p0, p2 - p0)
        ln = np.linalg.norm(n)
        if ln < 1e-9:
            continue
        n = n / ln
        d = -n @ p0
        inl = np.abs(P @ n + d) < eps
        c = int(inl.sum())
        if c < 0.03 * len(P):
            continue
        # 用内点精修一次
        Q = P[inl]
        n2 = np.linalg.svd(Q - Q.mean(0))[2][-1]
        d2 = -n2 @ Q.mean(0)
        c2 = int((np.abs(P @ n2 + d2) < eps).sum())
        if c2 > 0.9 * c:
            n, c = n2, c2
        best.append((c, n, d))
    best.sort(key=lambda x: -x[0])
    # 去重（法向相近的合并）
    out = []
    for c, n, d in best:
        if all(abs(n @ m) < 0.98 for _, m, _ in out):
            out.append((c, n, d))
        if len(out) >= 4:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--ply", required=True)
    args = ap.parse_args()

    cams = json.load(open(args.cameras))
    R = np.array([c["rotation"] for c in cams], float)
    upA = -R[:, :, 1].mean(0); upA /= np.linalg.norm(upA)
    M = R[:, :, 0].T @ R[:, :, 0]
    w, V = np.linalg.eigh(M)
    upB = V[:, 0]
    if upB @ upA < 0:
        upB = -upB
    print(f"upA = {np.round(upA,4)}   upB = {np.round(upB,4)}   夹角 {ang(upA,upB):.2f}°\n")

    # ---- ① look_at 重建 ----
    C = np.array(cams[0]["position"], float)
    target = C + np.array([0.5, 0, 0.5], float)  # 任取一个斜向目标
    for tag, up in (("A", upA), ("B", upB)):
        Rc = look_at_R(C, target, up)
        rollA = math.degrees(math.asin(np.clip(Rc[:, 0] @ upA, -1, 1)))
        rollB = math.degrees(math.asin(np.clip(Rc[:, 0] @ upB, -1, 1)))
        print(f"① 用 up{tag} 构造 look_at → 相对 upA 的 roll {rollA:+.2f}°，相对 upB 的 roll {rollB:+.2f}°")

    # ---- ② RANSAC 平面 ----
    from plyfile import PlyData
    v = PlyData.read(args.ply)["vertex"]
    P = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    if len(P) > 20000:
        P = P[np.random.default_rng(1).choice(len(P), 20000, replace=False)]
    planes = ransac_planes(P)
    print(f"\n② RANSAC 平面（点云 {len(P)} 采样，eps=0.02）：")
    for c, n, d in planes:
        if n @ upB < 0:
            n = -n
        print(f"   内点 {c:>6} ({100*c/len(P):4.1f}%)  法向 {np.round(n,3)}  "
              f"与upA {ang(n,upA):5.1f}°  与upB {ang(n,upB):5.1f}°  "
              f"与-modelY(-Y轴) {ang(n,[0,-1,0]):5.1f}°")
    print("\n判读：地面 → 法向应与重力几乎平行(≈0°)；墙面/桌面 → 法向应与重力几乎垂直(≈90°)")


if __name__ == "__main__":
    main()
