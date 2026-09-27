#!/usr/bin/env python3
"""诊断：up 估计的两种算法对比（M6.1 修正依据）。

A) 相机 up 均值：up = normalize(mean(-R[:,1]))   —— 当前实现
   问题：相机 +Y(下) 会随**俯仰(pitch)**改变，故均值被平均俯仰污染。
B) 右轴零空间：up = argmin_U Σ (R_i[:,0]·U)²     —— 俯仰不变
   依据：pitch 是绕相机右轴的旋转，**不改变右轴**；故右轴只受 roll 影响，
   当 roll≈0 时所有右轴都躺在水平面内 → U ⊥ 全部右轴。
"""
import argparse
import json
import math

import numpy as np


def ang(a, b):
    return math.degrees(math.acos(float(np.clip(a @ b, -1.0, 1.0))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", required=True)
    args = ap.parse_args()
    cams = json.load(open(args.cameras))
    R = np.array([c["rotation"] for c in cams], dtype=np.float64)  # (N,3,3)
    right, down, fwd = R[:, :, 0], R[:, :, 1], R[:, :, 2]

    upA = -down.mean(axis=0); upA /= np.linalg.norm(upA)
    M = right.T @ right
    w, V = np.linalg.eigh(M)          # 升序
    upB = V[:, 0]
    if upB @ upA < 0:
        upB = -upB

    print(f"N={len(cams)}")
    print(f"右轴协方差特征值(升序): {np.round(w, 4)}  "
          f"→ 最小/次小 = {w[0]/w[1]:.4f}（越小说明右轴越共面，零空间假设越成立）")
    print(f"A) 相机up均值   up = [{upA[0]:+.4f}, {upA[1]:+.4f}, {upA[2]:+.4f}]")
    print(f"B) 右轴零空间   up = [{upB[0]:+.4f}, {upB[1]:+.4f}, {upB[2]:+.4f}]")
    print(f"两者夹角 = {ang(upA, upB):.2f}°  ← 即 A 被平均俯仰污染的幅度")

    for tag, U in (("A(均值)", upA), ("B(零空间)", upB)):
        roll = np.degrees(np.arcsin(np.clip(right @ U, -1, 1)))
        pitch = np.degrees(np.arcsin(np.clip(fwd @ U, -1, 1)))
        print(f"  相对 {tag}: roll mean={roll.mean():+.2f}° std={roll.std():.2f}° | "
              f"pitch mean={pitch.mean():+.2f}° std={pitch.std():.2f}° "
              f"(pitch>0=朝上看)")


if __name__ == "__main__":
    main()
