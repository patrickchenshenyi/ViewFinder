#!/usr/bin/env python3
"""重建 contact sheet（保持 16:9 比例，修复旧版把 1280x720 压成 256x256 的变形）。

从 <indir>/*.png 里按前缀分族重拼，不重新渲染。
用法：
  python rebuild_sheets.py --indir /tmp/m6cand --cameras <cameras.json> --outdir /tmp/m6cand
"""
import argparse
import glob
import json
import os
import re

import numpy as np
from PIL import Image

CELL_W = 320
CELL_H = 180  # 16:9


def sheet(files, path, cols_max=12):
    if not files:
        return 0
    files = files[:cols_max]
    sheet_img = np.zeros((CELL_H, CELL_W * len(files), 3), np.uint8)
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGB").resize((CELL_W, CELL_H), Image.LANCZOS)
        sheet_img[:, i * CELL_W:(i + 1) * CELL_W] = np.asarray(im)
    Image.fromarray(sheet_img).save(path)
    return len(files)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--indir", required=True)
    ap.add_argument("--cameras", required=True, help="用于把族 A 的 kf<id> 块按轨迹顺序排")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()
    outdir = args.outdir or args.indir

    all_png = sorted(glob.glob(os.path.join(args.indir, "*.png")))
    all_png = [f for f in all_png if "contact_sheet" not in os.path.basename(f)]
    fam_a = [f for f in all_png if re.match(r"kf\d+_", os.path.basename(f))]
    fam_b = [f for f in all_png if re.match(r"s\d+_cam", os.path.basename(f))]

    # 族 A 按 kf id 排序（kf0_orig, kf0_right, kf0_left, ...）
    def key_a(f):
        m = re.match(r"kf(\d+)_(\w+)\.png", os.path.basename(f))
        order = {"orig": 0, "right": 1, "left": 2, "up": 3, "fwd": 4,
                 "yawp": 5, "yawn": 6, "pitchup": 7, "pitchdn": 8}
        return (int(m.group(1)), order.get(m.group(2), 99))
    fam_a.sort(key=key_a)
    fam_b.sort()

    nA = sheet(fam_a, os.path.join(outdir, "contact_sheet_A.png"))
    nB = sheet(fam_b, os.path.join(outdir, "contact_sheet_B.png"))
    print(f"[sheets] 族A {nA} 张、族B {nB} 张（{CELL_W}x{CELL_H} 每格，16:9 不变形）")
    print(f"[sheets] 总有效渲染 {len(all_png)} 张，来源 {args.indir}")


if __name__ == "__main__":
    main()
