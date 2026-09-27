#!/usr/bin/env python3
"""M6.0c/§4 补充：近轨迹"有效域"边界扫描 —— 回答"±0.4m 是不是选太小了"。

对分层关键帧，沿 5 个方向、按距离 0.25→3.0 递增扰动，统计每个距离档的
L0 有效率（tenengrad/dark%/白雾规则），找出"最后仍可用"的距离 → 用于设定候选范围。

用法：
  LD_PRELOAD=... python sweep_range.py --ply <ply> --cameras <cameras.json> \
      --thresholds /root/m6/l0_thresholds.json --outdir /tmp/m6sweep
"""
import argparse
import json
import math
import os

import numpy as np
from PIL import Image

import render_pose
import up_utils
from freeview_audit import img_stats


def rot_around(v, axis, th):
    axis = np.asarray(axis, np.float64); axis = axis / (np.linalg.norm(axis) + 1e-9)
    v = np.asarray(v, np.float64)
    return v * math.cos(th) + np.cross(axis, v) * math.sin(th) + axis * (axis @ v) * (1 - math.cos(th))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--thresholds", default=None)
    ap.add_argument("--outdir", default="/tmp/m6sweep")
    ap.add_argument("--dists", nargs="+", type=float, default=[0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0])
    ap.add_argument("--stride", type=int, default=20)
    ap.add_argument("--yaws", nargs="+", type=float, default=[15, 30, 45, 60])
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    ten_min, dark_max, fog = 10.0, 48.0, {"mean_gt": 85.0, "std_lt": 25.0}
    if args.thresholds and os.path.exists(args.thresholds):
        t = json.load(open(args.thresholds))["suggested_l0"]
        ten_min, dark_max, fog = t["ten_min"], t["dark_max"], t["fog_rule"]

    model = render_pose.load_model(args.ply)
    cams = json.load(open(args.cameras))
    up = up_utils.estimate_up(cams)
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    picked = cams[::args.stride]
    print(f"关键帧抽样 {len(picked)} 个（每 {args.stride} 取 1）；L0 阈值 "
          f"ten_min={ten_min:.1f} dark_max={dark_max:.1f}")

    def valid(s):
        return s["tenengrad"] >= ten_min and s["dark%"] <= dark_max and \
            not (s["mean"] > fog["mean_gt"] and s["std"] < fog["std_lt"])

    def run(R, C):
        img = render_pose.render_pose(model, render_pose.build_camera(R, C, fx, fy, w, h))
        return img_stats(img), img

    print(f"\n{'方向':>8}" + "".join(f"{d:>7}m" for d in args.dists))

    # 平移方向
    for dname in ("right", "left", "up", "fwd", "back"):
        line = f"{dname:>8}"
        for d in args.dists:
            nv = 0
            for c in picked:
                R = np.asarray(c["rotation"], np.float64)
                C = np.asarray(c["position"], np.float64)
                if dname == "right":
                    Cd = C + d * R[:, 0]
                elif dname == "left":
                    Cd = C - d * R[:, 0]
                elif dname == "up":
                    Cd = C + d * up
                elif dname == "fwd":
                    Cd = C + d * R[:, 2]
                else:
                    Cd = C - d * R[:, 2]
                s, _ = run(R, Cd)
                nv += valid(s)
            line += f"{100*nv/len(picked):>7.0f}%"
        print(line)

    # 偏航
    print()
    for yaw in args.yaws:
        line = f"yaw{yaw:>3.0f}°"
        for d in args.dists:
            nv = 0
            for c in picked:
                R = np.asarray(c["rotation"], np.float64)
                C = np.asarray(c["position"], np.float64)
                nf = rot_around(R[:, 2], up, math.radians(yaw))
                nr = rot_around(R[:, 0], up, math.radians(yaw))
                cy = np.cross(nf, nr)
                Rd = np.stack([nr, cy, nf], axis=1)
                # 偏航 + 平移 d（在原始右方向上）
                Cd = C + d * R[:, 0]
                s, _ = run(Rd, Cd)
                nv += valid(s)
            line += f"{100*nv/len(picked):>7.0f}%"
        print(line)
    print("\n判读：有效率首次明显掉的档位 ≈ 该方向的可用边界")


if __name__ == "__main__":
    main()
