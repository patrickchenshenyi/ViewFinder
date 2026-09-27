#!/usr/bin/env python3
"""M6.6.3 验收测试：VLM 成对门控 + 确定性退化守卫。

P1 好 vs 完全空（look_away）→ 守卫应触发（主体在相机后）
P2 好 vs 白雾（orbit_back2，structure 2.5）→ VLM 成对判定，期望判"好"
P3 好 vs 部分可见（orbit_back3，coverage 0.57）→ VLM 成对判定，期望判"好"
P4 好 vs 主体出画（强右转 70°）→ 守卫应触发
P5 好 vs 主体压出画（上移过头）→ 守卫应触发
P6（探索）近距候选 vs 远退含环境（back_3m_aim）→ 观察 VLM 偏好

判对率 = P1–P5 判"好"的比例（守卫触发算正确）。

用法（云端，需 LD_PRELOAD）：
  LD_PRELOAD=... python m663_test.py --ply <ply> --cameras <cameras.json> \
      --l1 /root/m6/l1_top_v2.json --key <KEY> --out /root/m6/m663_test.json
"""
import argparse
import json
import math

import numpy as np

import render_pose
import up_utils
from l1_score import cluster_subjects
from refine import pairwise_better, degenerate_guard, rot_around, compose_R
from freeview_audit import look_at
from plyfile import PlyData


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--l1", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--out", default="/root/m6/m663_test.json")
    args = ap.parse_args()

    model = render_pose.load_model(args.ply)
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    up = up_utils.estimate_up(cams)
    l1 = json.load(open(args.l1))
    by_name = {c["name"]: c for c in l1["top20"]}

    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    subjects = cluster_subjects(xyz)
    subj = subjects[by_name["s0_cam2_right3"]["subject"]]
    center = np.median(xyz, axis=0)
    robust_r = float(np.percentile(np.linalg.norm(xyz - center, axis=1), 99))

    R0 = np.asarray(cams[0]["rotation"], np.float64)
    C0 = np.asarray(cams[0]["position"], np.float64)
    fwd = R0[:, 2]

    def render(R, C):
        return render_pose.render_pose(model, render_pose.build_camera(R, C, fx, fy, w, h))

    good = by_name["s0_cam2_right3"]
    RG = np.asarray(good["R"], np.float64); CG = np.asarray(good["C"], np.float64)

    # ---- 坏视图构造 ----
    nf = -fwd; camY = np.cross(nf, R0[:, 0])
    R_away = np.stack([R0[:, 0], camY, nf], axis=1)                    # look_away

    e1 = np.array([1.0, 0.0, 0.0]); e1 = e1 - up * float(e1 @ up); e1 = e1 / np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    R_orbs, C_orbs = [], []
    for az in (2.0, 3.0):
        C = center + 2.5 * robust_r * (math.cos(az) * e1 + math.sin(az) * e2)
        R_orbs.append(look_at(C, center, up)); C_orbs.append(C)

    th = math.radians(70)
    nf2 = rot_around(RG[:, 2], up, th); nr2 = rot_around(RG[:, 0], up, th)
    R_yaw = compose_R(nr2, nf2)                                        # 主体出画

    depth = float((subj["center"] - CG) @ RG[:, 2])
    C_up = CG + up * 1.2 * depth                                        # 主体压出画

    C_back3 = C0 - 3.0 * fwd
    R_back3 = look_at(C_back3, center, up)                              # 远退含环境

    pairs = [
        ("P1 好vs完全空(guard)", (RG, CG), (R_away, C0), "guard"),
        ("P2 好vs白雾(vlm)", (RG, CG), (R_orbs[0], C_orbs[0]), "vlm"),
        ("P3 好vs部分可见(vlm)", (RG, CG), (R_orbs[1], C_orbs[1]), "vlm"),
        ("P4 好vs主体出画(guard)", (RG, CG), (R_yaw, CG), "guard"),
        ("P5 好vs主体压出画(guard)", (RG, CG), (RG, C_up), "guard"),
    ]
    results = {}
    correct = 0
    for tag, (RA, CA), (RB, CB), kind in pairs:
        g = degenerate_guard(subj, RB, CB, fx, fy, w, h)
        if g:
            print(f"{tag}: 守卫触发（{g}）→ 判'好' ✅")
            results[tag] = {"guard": g, "correct": True}
            correct += 1
            continue
        vd = pairwise_better(render(RA, CA), render(RB, CB), args.key)
        ok = vd == "old"
        print(f"{tag}: VLM 成对判定={vd} → {'✅' if ok else '❌'}")
        results[tag] = {"verdict": vd, "correct": ok}
        correct += ok
    print(f"\n判对率 {correct}/5 = {100*correct/5:.0f}%")

    v6 = pairwise_better(render(RG, CG), render(R_back3, C_back3), args.key)
    note = ("new=偏好远退含环境 → 门控不公式化 ✅" if v6 == "new" else
            "old=偏好近距 / tie=不分胜负（不构成误杀，只说明本对偏好）")
    print(f"P6（探索）近距 vs 远退含环境：VLM 判定={v6}（{note}）")
    results["P6_explore"] = v6

    json.dump(results, open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"\n已写 {args.out}")


if __name__ == "__main__":
    main()
