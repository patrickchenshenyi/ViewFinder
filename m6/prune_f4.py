#!/usr/bin/env python3
"""M6.0c F4: 视图一致性（view-consistency）游离高斯清理。

原理：一个真实表面上的高斯，必然被**多个**训练视角看到（可见）；而游离/雾状浮尘
往往只在极少数视角偶然可见。统计每个高斯在采样训练相机上的可见次数，
只保留被 >= --min-views 个视角看到的高斯。

用法（云端，需 LD_PRELOAD）：
  python prune_f4.py --ply <input.ply> --cameras <cameras.json> \
      --out <pruned.ply> [--step 4] [--min-views 2] [--max-views 120]
"""
import argparse
import json

import numpy as np
import torch
from plyfile import PlyData

import render_pose
from gaussian_splatting.gaussian_renderer import render


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--step", type=int, default=4, help="每 N 个训练相机取 1 个（控耗时）")
    ap.add_argument("--min-views", type=int, default=2, help="可见次数 < 该值则视为浮尘删除")
    ap.add_argument("--max-views", type=int, default=120, help="采样相机上限（防超长序列）")
    args = ap.parse_args()

    model = render_pose.load_model(args.ply)
    N = len(model.get_xyz)
    cams = json.load(open(args.cameras))
    sample = cams[:: args.step][: args.max_views]
    print(f"[f4] N={N:,}  采样相机 {len(sample)}/{len(cams)}（step={args.step}）")

    seen = np.zeros(N, dtype=np.int32)
    pipe = type("P", (), {"convert_SHs_python": False, "compute_cov3D_python": False})()
    bg = torch.zeros(3, dtype=torch.float32, device="cuda")
    for i, c in enumerate(sample):
        R = np.asarray(c["rotation"], np.float32)
        C = np.asarray(c["position"], np.float32)
        fx = c.get("fx", cams[0]["fx"]); fy = c.get("fy", cams[0]["fy"])
        w = c.get("width", cams[0]["width"]); h = c.get("height", cams[0]["height"])
        cam = render_pose.build_camera(R, C, fx, fy, w, h)
        with torch.no_grad():
            out = render(cam, model, pipe, bg, scaling_modifier=1.0)
        vis = out["visibility_filter"].detach().cpu().numpy()
        seen += vis
        if (i + 1) % 20 == 0:
            print(f"[f4]   已处理 {i+1}/{len(sample)} 相机")

    keep = seen >= args.min_views
    n_drop = int((~keep).sum())
    print(f"[f4] 可见次数分布: 0次={int((seen==0).sum()):,}  1次={int((seen==1).sum()):,}  "
          f"2次={int((seen==2).sum()):,}  3-10次={int(((seen>=3)&(seen<=10)).sum()):,}  "
          f">10次={int((seen>10).sum()):,}")
    print(f"[f4] 删除 <{args.min_views} 次的浮尘: {n_drop:,}（{100*n_drop/N:.2f}%），"
          f"保留 {int(keep.sum()):,}")

    ply = PlyData.read(args.ply)
    ply["vertex"].data = ply["vertex"].data[keep]
    ply.write(args.out)
    print(f"[f4] 已写 {args.out}")


if __name__ == "__main__":
    main()
