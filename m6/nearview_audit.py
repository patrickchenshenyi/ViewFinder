#!/usr/bin/env python3
"""M6.0c 补充：近轨迹扰动视角体检 —— 判断"轨迹邻域"是否是可行的候选空间。

对若干训练相机，施加 ±0.4m 平移 / ±15° 偏航的小扰动，渲染并量化，
对比未扰动训练视角。若近轨迹视角 tenengrad/dark% 与训练视角同量级 → M6 可行。

用法：
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python nearview_audit.py --ply <ply> --cameras <cameras.json> --outdir /tmp/m6c_near
"""
import argparse
import json
import math
import os

import numpy as np
from PIL import Image

import render_pose
from freeview_audit import img_stats, look_at, orthonormal_basis


def rot_around(v, axis, th):
    axis = np.asarray(axis, np.float64); axis = axis / (np.linalg.norm(axis) + 1e-9)
    v = np.asarray(v, np.float64)
    return (v * math.cos(th) + np.cross(axis, v) * math.sin(th)
            + axis * (axis @ v) * (1 - math.cos(th)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--outdir", default="/tmp/m6c_near")
    ap.add_argument("--ids", nargs="+", type=int, default=[0, 45, 90, 135])
    ap.add_argument("--sh", type=int, default=3)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    model = render_pose.load_model(args.ply, sh_degree=args.sh)
    with open(args.cameras) as f:
        cams = json.load(f)
    by_id = {c["id"]: c for c in cams}

    ups = []
    for c in cams:
        R = np.asarray(c["rotation"], np.float32)
        ups.append(-R[:, 1])
    up = np.mean(ups, axis=0); up = up / (np.linalg.norm(up) + 1e-9)

    fx = cams[0]["fx"]; fy = cams[0]["fy"]; w = cams[0]["width"]; h = cams[0]["height"]
    D = 0.4  # 平移扰动（米，SLAM 尺度）
    YAW = math.radians(15)

    missing = [i for i in args.ids if i not in by_id]
    if missing:
        print(f"[warn] 请求的 id 不存在、已显式跳过: {missing}"
              f"（cameras.json ids {min(by_id)}..{max(by_id)}，共 {len(by_id)} 个）")
    resolved = [i for i in args.ids if i in by_id]
    print(f"[baseline] 实际渲染的相机 id = {resolved}")

    print(f"{'view':>22}{'mean':>8}{'std':>8}{'dark%':>8}{'tenengrad':>11}")
    rows = []
    for cid in resolved:
        c = by_id[cid]
        R = np.asarray(c["rotation"], np.float32).astype(np.float64)
        C = np.asarray(c["position"], np.float32).astype(np.float64)
        fwd = R[:, 2]; right = R[:, 0]

        def render(R_, C_, name):
            cam = render_pose.build_camera(R_.astype(np.float32), C_.astype(np.float32),
                                           fx, fy, w, h)
            img = render_pose.render_pose(model, cam)
            s = img_stats(img)
            print(f"{name:>22}{s['mean']:>8.1f}{s['std']:>8.1f}{s['dark%']:>8.1f}{s['tenengrad']:>11.1f}")
            Image.fromarray(img).save(os.path.join(args.outdir, f"{name}.png"))
            return img

        render(R, C, f"train_{cid}")
        # 平移扰动
        render(R, C + D * right, f"t{cid}_right{D}")
        render(R, C - D * right, f"t{cid}_left{D}")
        render(R, C + D * up, f"t{cid}_up{D}")
        render(R, C + D * fwd, f"t{cid}_fwd{D}")
        render(R, C - D * fwd, f"t{cid}_back{D}")
        # 偏航扰动（绕 up 轴转 forward/right）
        for sign, tag in [(1, "yawp"), (-1, "yawn")]:
            nf = rot_around(fwd, up, sign * YAW)
            nr = rot_around(right, up, sign * YAW)
            R_yaw = np.stack([nr, np.cross(nf, nr), nf], axis=1).astype(np.float64)
            render(R_yaw, C, f"t{cid}_{tag}")


if __name__ == "__main__":
    main()
