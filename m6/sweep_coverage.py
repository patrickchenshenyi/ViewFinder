#!/usr/bin/env python3
"""M6.6.4: 除雾模型有效域重测（判据 = coverage 可判读性，M6.6.2 新判据）。

扫「距离 × 方向」（平移后**重新瞄准场景中心**，模拟真实摄影师后退/横移再取景）
+ 「偏航」（原地转视角），统计每格可判读率，产出方向相关的距离上限。

用法（云端，需 LD_PRELOAD）：
  LD_PRELOAD=... python sweep_coverage.py --ply /root/m6/m6_fog_pruned.ply \
      --cameras <cameras.json> --out /root/m6/sweep_cov.json [--stride 30]
"""
import argparse
import json
import math

import numpy as np

import render_pose
import up_utils
from coverage import render_magenta, render_green, coverage_metrics
from freeview_audit import look_at
from plyfile import PlyData


def rot_around(v, axis, th):
    axis = np.asarray(axis, np.float64); axis = axis / (np.linalg.norm(axis) + 1e-9)
    v = np.asarray(v, np.float64)
    return v * math.cos(th) + np.cross(axis, v) * math.sin(th) + axis * (axis @ v) * (1 - math.cos(th))


def compose_R(right, fwd):
    right = right / (np.linalg.norm(right) + 1e-9)
    fwd = fwd / (np.linalg.norm(fwd) + 1e-9)
    return np.stack([right, np.cross(fwd, right), fwd], axis=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--out", default="/root/m6/sweep_cov.json")
    ap.add_argument("--stride", type=int, default=30)
    ap.add_argument("--dists", nargs="+", type=float, default=[0.5, 1.0, 1.5, 2.0, 3.0, 5.0])
    ap.add_argument("--yaws", nargs="+", type=float, default=[30.0, 60.0])
    args = ap.parse_args()

    model = render_pose.load_model(args.ply)
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    up = up_utils.estimate_up(cams)
    picked = cams[::args.stride]
    print(f"[sweep] 关键帧抽样 {len(picked)} 个（每 {args.stride} 取 1）；"
          f"判据=coverage 可判读性（coverage≥5% 且 structure≥3）")

    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    center = np.median(xyz, axis=0)

    def judgeable_ratio(poses):
        n = 0
        for R, C in poses:
            img_m = render_magenta(model, R, C, fx, fy, w, h)
            img_g = render_green(model, R, C, fx, fy, w, h)
            n += coverage_metrics(img_m, img_g)["judgeable"]
        return n / max(len(poses), 1)

    def cell_of(make_pose):
        out = {}
        for d in args.dists:
            poses = [make_pose(c, d) for c in picked]
            out[d] = judgeable_ratio(poses)
        return out

    dirs = {
        "right": lambda c, d: (look_at(c["position"] + d * np.asarray(c["rotation"], np.float64)[:, 0], center, up),
                               c["position"] + d * np.asarray(c["rotation"], np.float64)[:, 0]),
        "left": lambda c, d: (look_at(c["position"] - d * np.asarray(c["rotation"], np.float64)[:, 0], center, up),
                              c["position"] - d * np.asarray(c["rotation"], np.float64)[:, 0]),
        "up": lambda c, d: (look_at(c["position"] + d * up, center, up),
                            c["position"] + d * up),
        "fwd": lambda c, d: (look_at(c["position"] + d * np.asarray(c["rotation"], np.float64)[:, 2], center, up),
                             c["position"] + d * np.asarray(c["rotation"], np.float64)[:, 2]),
        "back": lambda c, d: (look_at(c["position"] - d * np.asarray(c["rotation"], np.float64)[:, 2], center, up),
                              c["position"] - d * np.asarray(c["rotation"], np.float64)[:, 2]),
    }

    table = {}
    print(f"\n{'方向':>6}" + "".join(f"{d:>7}m" for d in args.dists))
    for dname, mk in dirs.items():
        table[dname] = cell_of(mk)
        print(f"{dname:>6}" + "".join(f"{100*table[dname][d]:>6.0f}%" for d in args.dists))

    # 偏航（原地转视角，不重新瞄准）
    yaw_table = {}
    print("\n偏航（原地）：")
    for yaw in args.yaws:
        for sign in (+1.0, -1.0):
            def mk(c, d, yaw=yaw, sign=sign):
                R = np.asarray(c["rotation"], np.float64)
                nf = rot_around(R[:, 2], up, sign * math.radians(yaw))
                nr = rot_around(R[:, 0], up, sign * math.radians(yaw))
                return compose_R(nr, nf), np.asarray(c["position"], np.float64)
            poses = [mk(c, None) for c in picked]
            key = f"{sign*yaw:+.0f}°"
            yaw_table[key] = judgeable_ratio(poses)
            print(f"  yaw {key}: 可判读率 {100*yaw_table[key]:.0f}%")

    # 方向相关的距离上限
    def limit(row, thresh):
        ds = sorted(row.keys())
        best = None
        for d in ds:
            if row[d] >= thresh:
                best = d
        return best

    limits = {dname: {"limit_80": limit(row, 0.8), "limit_50": limit(row, 0.5)}
              for dname, row in table.items()}
    print("\n方向距离上限（可判读率 ≥80% / ≥50% 的最远距离）：")
    for dname, L in limits.items():
        print(f"  {dname:<6} limit_80={L['limit_80']}m   limit_50={L['limit_50']}m")

    json.dump({"table": {k: {str(d): v for d, v in row.items()} for k, row in table.items()},
               "yaw": yaw_table, "limits": limits, "center": center.tolist()},
              open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"\n[sweep] 已写 {args.out}")


if __name__ == "__main__":
    main()
