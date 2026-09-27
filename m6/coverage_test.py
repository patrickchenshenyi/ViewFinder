#!/usr/bin/env python3
"""M6.6.2 验收测试：coverage 可判读性 vs 旧 L0，在四类视图上对比。

视图类别：好视角（训练/族B）、黑屏（轨道背面）、白雾（后退0.4m）、远退（2/3/4m，用户式）。
验收：远退类 coverage≈40-60% → **不被否决**；黑屏 coverage≈0 → 否决；白雾 structure≈0 → 否决。

用法（云端，需 LD_PRELOAD）：
  LD_PRELOAD=... python coverage_test.py --ply <ply> --cameras <cameras.json> \
      --l1 /root/m6/l1_top_v2.json --out /root/m6/coverage_test.json
"""
import argparse
import json
import math

import numpy as np
from PIL import Image

import render_pose
import up_utils
from coverage import render_magenta, render_green, coverage_metrics
from freeview_audit import img_stats, look_at


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--l1", required=True)
    ap.add_argument("--out", default="/root/m6/coverage_test.json")
    args = ap.parse_args()

    model = render_pose.load_model(args.ply)
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    up = up_utils.estimate_up(cams)
    by_name = {c["name"]: c for c in json.load(open(args.l1))["top20"]}

    from plyfile import PlyData
    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    center = np.median(xyz, axis=0)
    robust_r = float(np.percentile(np.linalg.norm(xyz - center, axis=1), 99))

    R0 = np.asarray(cams[0]["rotation"], np.float64)
    C0 = np.asarray(cams[0]["position"], np.float64)
    fwd = R0[:, 2]

    views = []
    # 好视角 1：训练位姿
    views.append(("train_kf0", R0, C0))
    # 好视角 2：族 B 候选
    if "s0_cam2_right3" in by_name:
        c = by_name["s0_cam2_right3"]
        views.append(("B_s0_cam2_right3", np.asarray(c["R"], np.float64),
                      np.asarray(c["C"], np.float64)))
    # 黑屏：轨道背面（场景后方远距）
    e1 = np.array([1.0, 0.0, 0.0])
    e1 = e1 - up * float(e1 @ up); e1 = e1 / np.linalg.norm(e1)
    e2 = np.cross(up, e1)
    for az in (2.0, 3.0):  # 两个背面方位
        C = center + 2.5 * robust_r * (math.cos(az) * e1 + math.sin(az) * e2)
        views.append((f"orbit_back{az:.0f}", look_at(C, center, up), C))
    # 白雾：后退 0.4m（保持朝向）
    views.append(("back_0.4m", R0, C0 - 0.4 * fwd))
    # 远退（用户式）：后退 2/3/4m，重新看向场景中心
    for d in (2.0, 3.0, 4.0):
        C = C0 - d * fwd
        views.append((f"back_{d:.0f}m_aim", look_at(C, center, up), C))
    # 完全不可判读：背对场景看空处
    nf = -fwd
    camY = np.cross(nf, R0[:, 0])
    R_away = np.stack([R0[:, 0], camY, nf], axis=1)
    views.append(("look_away", R_away, C0))

    print(f"{'view':<18}{'coverage':>9}{'struct':>8}{'可判读':>7} | "
          f"{'旧L0 ten/dark/fog':>28}{'旧L0判定':>8}")
    out = {}
    for name, R, C in views:
        img_m = render_magenta(model, R, C, fx, fy, w, h)
        img_g = render_green(model, R, C, fx, fy, w, h)
        cm = coverage_metrics(img_m, img_g)
        img_b = render_pose.render_pose(model, render_pose.build_camera(R, C, fx, fy, w, h))
        s = img_stats(img_b)
        old_ok = s["tenengrad"] >= 10.2 and s["dark%"] <= 48.2 and \
            not (s["mean"] > 85 and s["std"] < 25)
        print(f"{name:<18}{cm['coverage']:>9.2f}{cm['structure']:>8.2f}"
              f"{'✅' if cm['judgeable'] else '❌':>7} | "
              f"{s['tenengrad']:>8.1f}/{s['dark%']:.0f}%/雾:{s['mean']>85 and s['std']<25}"
              f"{'PASS' if old_ok else 'FAIL':>17}")
        out[name] = {"coverage": cm, "old": s, "old_verdict": old_ok}
    json.dump(out, open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"\n[coverage] 已写 {args.out}")

    # ---- 合成单测：双背景防"场景含品红/绿内容被误判空" ----
    synth_m = np.zeros((120, 120, 3), np.uint8)
    synth_m[20:50, 20:50] = (255, 0, 255)      # 品红块（在品红背景渲染中"隐形"）
    synth_m[70:100, 70:100] = (0, 255, 0)      # 绿块
    synth_m[20:50, 70:100] = (128, 128, 128)   # 灰块
    synth_g = synth_m.copy()                    # 同一场景在绿背景下的渲染：品红块可见
    single = coverage_metrics(synth_m)
    dual = coverage_metrics(synth_m, synth_g)
    print(f"[unit] 场景含品红/绿块：单背景 coverage={single['coverage']:.3f}"
          f"（品红块被误判空）vs 双背景 coverage={dual['coverage']:.3f}（全部判覆盖）→ "
          f"{'PASS ✅' if dual['coverage'] > single['coverage'] else 'FAIL ❌'}")


if __name__ == "__main__":
    main()
