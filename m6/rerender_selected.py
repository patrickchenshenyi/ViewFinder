#!/usr/bin/env python3
"""M6.6.8: 两段式渲染第二段 —— 只对 screen 选出的桶代表做全分辨率重渲染。

第一段 candidate_gen2 --scale 0.5 只粗渲（coverage/裁切/筛选用）；
本脚本读 candidates.json（含 R/C/coverage/crops）+ screen.json（selected 名字），
按全分辨率重渲染这些候选的展示图与裁切变体，供 vlm_score 看高清图打分。

用法（云端，需 LD_PRELOAD）：
  python rerender_selected.py --ply <ply> --cameras <cameras.json> \
      --candidates <candidates.json> --selection <screen.json> \
      --outdir <full-res 渲染目录>
"""
import argparse
import json
import os

import numpy as np
from PIL import Image

import render_pose
from coverage import render_white, BLACK, WHITE, coverage_mask, coverage_metrics
from freeview_audit import img_stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--selection", required=True)
    ap.add_argument("--outdir", required=True)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    model = render_pose.load_model(args.ply)
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]

    cands = json.load(open(args.candidates))
    cand_scale = float(cands.get("scale", 1.0))
    by_name = {r["name"]: r for r in cands["results"]}

    sel = json.load(open(args.selection))
    selected = sel.get("selected") or sel.get("top20") or []
    print(f"[rerender] 全分辨率重渲染 {len(selected)} 个桶代表（粗渲倍率 {cand_scale}）")

    n_ok = 0
    for c in selected:
        r = by_name.get(c["name"])
        if r is None:
            print(f"[rerender] 警告：候选 {c['name']} 不在 candidates.json")
            continue
        R = np.asarray(r["R"], np.float64)
        C = np.asarray(r["C"], np.float64)
        # 粗渲坐标系下的 crops 还原到全分辨率
        inv = 1.0 / cand_scale
        crops = {cn: [int(round(v * inv)) for v in b]
                 for cn, b in (r.get("crops") or {}).items()}

        img_b = render_pose.render_pose(model, render_pose.build_camera(R, C, fx, fy, w, h))
        img_w = render_white(model, R, C, fx, fy, w, h)
        cm = coverage_metrics(img_b, img_w, bg_a=BLACK, bg_b=WHITE)
        cov_b = float(coverage_mask(img_b, None, bg_a=BLACK).mean())
        cov_w = float(coverage_mask(img_w, None, bg_a=WHITE).mean())
        disp = img_b if cov_b >= cov_w else img_w

        # 与 vlm_score 同款规则：低覆盖率候选换 crop_43 裁切图（裁掉残缺边）
        if r.get("coverage", {}).get("coverage", 1.0) < 0.7:
            b = crops.get("crop_43")
            if b is not None and b[2] > b[0] and b[3] > b[1]:
                Image.fromarray(disp[b[1]:b[3], b[0]:b[2]]).save(
                    os.path.join(args.outdir, f"{c['name']}.png"))
                n_ok += 1
                continue
        Image.fromarray(disp).save(os.path.join(args.outdir, f"{c['name']}.png"))
        n_ok += 1
        # 其余裁切变体（vlm 只用 crop_43，但保留一致输出便于排查）
        for cn, b in crops.items():
            if cn in ("full", "crop_43"):
                continue
            if b[2] > b[0] and b[3] > b[1]:
                Image.fromarray(disp[b[1]:b[3], b[0]:b[2]]).save(
                    os.path.join(args.outdir, f"{c['name']}_{cn}.png"))

    print(f"[rerender] 完成 {n_ok}/{len(selected)} → {args.outdir}")


if __name__ == "__main__":
    main()
