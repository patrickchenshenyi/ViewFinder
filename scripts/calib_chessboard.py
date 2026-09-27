#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calib_chessboard.py —— 用棋盘格实拍标定，得到【真实喂给 SLAM 的那一帧坐标系】的内参。

为什么还需要它（`android_intrinsics.py` 不是已经给了吗）：
  出厂标定（LENS_INTRINSIC_CALIBRATION）只描述「传感器全像素阵列」。
  从传感器到你的 1280x720 帧，中间还隔着：居中裁剪 → 缩放 → 可能的旋转 →
  厂商的畸变校正 / EIS / 数字变焦。这些静态参数**给不出来**。
  只有拍一次棋盘格，才能一次性把这一整条链路全部包进去。

用法：
  # 0) 依赖（本仓库已备好 venv）
  tools/_venv/bin/python scripts/calib_chessboard.py --help

  # 1) 拍：打印一张 9x6 格（内角点 8x5）的棋盘，格边长量一下，比如 25mm
  #    用【和实际建图完全一样的方式】拍：同分辨率、同方向、同 App（IP Webcam 就开 1280x720）
  #    扫动 20~30 个姿态，务必让棋盘出现在画面的四个角 + 中心 + 倾斜

  # 2) 抽帧（视频直接喂，脚本内部抽帧；也可以先 ffmpeg 抽成 png）
  tools/_venv/bin/python scripts/calib_chessboard.py \
      --video /path/to/calib.mp4 --every 5 --pattern 8x5 --square 25 \
      --expect 892.400,892.253,632.984,357.109

  # 3) 认可结果后写回 YAML（只改数字，保留注释）
  tools/_venv/bin/python scripts/calib_chessboard.py --frames /tmp/calib \
      --pattern 8x5 --square 25 --patch photo_slam_config/phone_1280x720.yaml

  # 4) 不相信脚本本身？跑合成自检（已知内参 → 渲染 → 标定 → 比对）
  tools/_venv/bin/python scripts/calib_chessboard.py --selftest

判读结果：
  * RMS 重投影误差 < 0.5px 好，0.5~1.0 勉强，> 1.0 重拍（角点少 / 覆盖差 / 糊了）
  * fx ≈ fy：差值 > 5% 说明画面被【各向异性】拉伸过（例如 4:3 拉成 16:9）——
    脚本会显式提示把 --crop / --center-crop 换成 4:3 重算静态值
  * 与 --expect（出厂标定换算值）差 < 5% → 两边互相印证，可以放心用
  * |k1| > 0.5 → 手机广角畸变很大，ORB-SLAM3 的 PinHole 模型可能吃不消，先 undistort 再喂
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import tempfile

import cv2
import numpy as np


# --------------------------------------------------------------------------------------
# 角点检测
# --------------------------------------------------------------------------------------

def find_corners(gray: np.ndarray, pattern: tuple[int, int]) -> np.ndarray | None:
    """返回 (N,1,2) 亚像素角点，或 None。优先用 SB（更稳），失败退回经典法。

    注意：OpenCV 5.0 起两个检测器都返回 (N,2)（4.x 是 (N,1,2)）。
    必须统一 reshape 成 (N,1,2)，否则后面 `proj - corners` 会被广播成 (N,N,2)，
    算出来的「逐帧误差」变成角点两两平均距离（看着像 100+px 的假坏帧）。
    """
    ok, corners = False, None
    if hasattr(cv2, "findChessboardCornersSB"):
        ok, corners = cv2.findChessboardCornersSB(
            gray, pattern, flags=cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
        )
    if not ok:
        ok, corners = cv2.findChessboardCorners(
            gray, pattern, flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
        )
        if ok:
            corners = cv2.cornerSubPix(
                gray, corners, (11, 11), (-1, -1),
                (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001),
            )
    if not ok or corners is None:
        return None
    return np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2)


def coverage(corners: np.ndarray, size: tuple[int, int]) -> float:
    """角点包围盒面积 / 画面面积。用来看「有没有扫到四角」，太小则标定不可信。"""
    w, h = size
    x0, y0 = corners.reshape(-1, 2).min(axis=0)
    x1, y1 = corners.reshape(-1, 2).max(axis=0)
    return float(max(0.0, x1 - x0) * max(0.0, y1 - y0)) / float(w * h)


# --------------------------------------------------------------------------------------
# 取帧
# --------------------------------------------------------------------------------------

def frames_from_video(path: str, every: int, limit: int):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"打不开视频：{path}")
    i = 0
    n = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i % every == 0:
                yield f"{os.path.basename(path)}#{i}", frame
                n += 1
                if limit and n >= limit:
                    break
            i += 1
    finally:
        cap.release()


def frames_from_dir(path: str, limit: int):
    exts = ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.webp")
    files: list[str] = []
    for e in exts:
        files += glob.glob(os.path.join(path, e)) + glob.glob(os.path.join(path, e.upper()))
    for f in sorted(set(files))[:limit or None]:
        img = cv2.imread(f)
        if img is not None:
            yield os.path.basename(f), img


# --------------------------------------------------------------------------------------
# 标定
# --------------------------------------------------------------------------------------

def calibrate(views, pattern, square, verbose=True):
    objp = np.zeros((pattern[0] * pattern[1], 3), np.float32)
    objp[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2) * square

    obj_pts, img_pts, used, size, covs = [], [], [], None, []
    for name, img in views:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if size is None:
            size = (gray.shape[1], gray.shape[0])
        elif (gray.shape[1], gray.shape[0]) != size:
            print(f"  [skip] {name}: 尺寸 {gray.shape[1]}x{gray.shape[0]} != {size[0]}x{size[1]}")
            continue
        c = find_corners(gray, pattern)
        if c is None:
            if verbose:
                print(f"  [miss] {name}: 没找到棋盘格")
            continue
        cov = coverage(c, size)
        if cov < 0.03:                     # 棋盘在一小块区域里 → 对畸变没约束力
            if verbose:
                print(f"  [skip] {name}: 角点只覆盖画面 {cov * 100:.1f}%，太小")
            continue
        obj_pts.append(objp)
        img_pts.append(c)
        used.append(name)
        covs.append(cov)
        if verbose:
            print(f"  [ok]   {name}: 覆盖 {cov * 100:.1f}%")

    if len(obj_pts) < 8:
        raise SystemExit(
            f"只成功用了 {len(obj_pts)} 张（需要 ≥8）。"
            "常见原因：棋盘格内角点数写错（--pattern）、棋盘太糊、没扫到画面四角。"
        )

    rms, K, D, rvecs, tvecs = cv2.calibrateCamera(
        obj_pts, img_pts, size, None, None, flags=0,   # 5 参数全放开；要固定 k3 用 cv2.CALIB_FIX_K3
    )

    # 每张图的重投影误差，用来揪出坏样本
    per_view = []
    for i in range(len(obj_pts)):
        proj, _ = cv2.projectPoints(obj_pts[i], rvecs[i], tvecs[i], K, D)
        per_view.append((used[i], float(np.linalg.norm(proj - img_pts[i], axis=2).mean())))

    return {
        "rms": float(rms), "K": K, "D": D.ravel(), "size": size,
        "n_used": len(obj_pts), "coverage_max": max(covs), "per_view": per_view,
    }


def report(res, expect=None):
    K, D = res["K"], res["D"]
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    w, h = res["size"]
    k1, k2, p1, p2 = D[0], D[1], D[2], D[3]
    k3 = D[4] if len(D) > 4 else 0.0

    print(f"\n== 标定结果（{w}x{h}，用了 {res['n_used']} 张，最大覆盖 {res['coverage_max'] * 100:.0f}%）==")
    print(f"RMS 重投影误差 = {res['rms']:.3f} px", end="  ")
    print("→ 好" if res["rms"] < 0.5 else ("→ 勉强可用" if res["rms"] < 1.0 else "→ 偏差大，建议重拍"))
    print(f"fx={fx:.3f}  fy={fy:.3f}  cx={cx:.3f}  cy={cy:.3f}")
    print(f"k1={k1:.6f}  k2={k2:.6f}  p1={p1:.6f}  p2={p2:.6f}  k3={k3:.6f}")
    print(f"水平 FOV ≈ {2 * np.degrees(np.arctan(w / (2 * fx))):.1f}°   "
          f"垂直 FOV ≈ {2 * np.degrees(np.arctan(h / (2 * fy))):.1f}°")

    bad = [x for x in res["per_view"] if x[1] > max(1.0, res["rms"] * 3)]
    if bad:
        print("  误差偏大的帧（考虑删掉重跑）：" + ", ".join(f"{n}({e:.2f}px)" for n, e in bad[:5]))

    if abs(fx / fy - 1) > 0.05:
        print(f"  [warn] fx/fy = {fx / fy:.3f}，差 >5% → 画面被各向异性拉伸过。")
        print("         典型场景：4:3 传感器出 16:9 时不是居中裁剪而是硬拉伸。")
        print("         对策：android_intrinsics.py 改用 --center-crop 4:3（或直接不裁）重算静态值。")

    if expect:
        ex = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", expect)]
        if len(ex) >= 4:
            print("\n== 与出厂标定换算值对比 ==")
            for name, got, want in (("fx", fx, ex[0]), ("fy", fy, ex[1]),
                                    ("cx", cx, ex[2]), ("cy", cy, ex[3])):
                d = (got - want) / want * 100 if want else float("nan")
                flag = "  ✅" if abs(d) < 5 else ("  ⚠️" if abs(d) < 15 else "  ❌")
                print(f"  {name}: 标定 {got:8.3f}   出厂 {want:8.3f}   差 {d:+6.2f}%{flag}")
            print("  <5%：互相印证，可放心用；>15%：八成是裁剪/旋转/变焦假设错了，先别信任何一个")

    if abs(k1) > 0.5:
        print(f"\n  [warn] |k1|={abs(k1):.3f} 很大（手机广角常见 -0.2~0）。"
              "ORB-SLAM3 的 PinHole 吃不下太大畸变 → 先用 undistort 把帧矫直再喂。")

    print("\n== YAML ==")
    print(f"Camera1.fx: {fx:.6f}\nCamera1.fy: {fy:.6f}\nCamera1.cx: {cx:.6f}\nCamera1.cy: {cy:.6f}")
    print(f"Camera1.k1: {k1:.6f}\nCamera1.k2: {k2:.6f}\nCamera1.p1: {p1:.6f}\n"
          f"Camera1.p2: {p2:.6f}\nCamera1.k3: {k3:.6f}")
    print(f"Camera.width: {w}\nCamera.height: {h}")
    return [fx, fy, cx, cy, k1, k2, p1, p2, k3]


def patch_yaml(path: str, vals) -> None:
    keys = ["Camera1.fx", "Camera1.fy", "Camera1.cx", "Camera1.cy",
            "Camera1.k1", "Camera1.k2", "Camera1.p1", "Camera1.p2", "Camera1.k3"]
    repl = dict(zip(keys, vals))
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    changed, out = [], []
    for line in lines:
        m = re.match(r"^(\s*)([A-Za-z0-9_.]+)(\s*:\s*)([-+0-9.eE]*)([^\n]*)(\n?)$", line)
        if m and m.group(2) in repl:
            key, old = m.group(2), m.group(4)
            new = f"{repl[key]:.6f}"
            if old != new:
                changed.append(f"  {key}: {old} -> {new}")
                line = f"{m.group(1)}{key}{m.group(3)}{new}{m.group(5)}{m.group(6)}"
        out.append(line)
    if not changed:
        print(f"[patch] {path} 无需修改")
        return
    print(f"[patch] {path} 将修改：\n" + "\n".join(changed))
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    print(f"[patch] 已写入 {path}")


# --------------------------------------------------------------------------------------
# 合成自检：已知 K → 渲染棋盘格 → 标定 → 看能不能恢复
# --------------------------------------------------------------------------------------

def selftest(pattern=(8, 5), square=0.025, n_views=20, seed=0, dist=True) -> bool:
    w, h = 1280, 720
    K_true = np.array([[892.400, 0, 632.984], [0, 892.253, 357.109], [0, 0, 1.0]])
    D_true = np.array([-0.12, 0.015, 0.0008, -0.0004, 0.0]) if dist else np.zeros(5)
    sq_px = 40                                     # 纹理里每格 40px
    tex_w, tex_h = (pattern[0] + 1) * sq_px, (pattern[1] + 1) * sq_px
    tex = np.full((tex_h, tex_w), 255, np.uint8)
    for j in range(pattern[1] + 1):
        for i in range(pattern[0] + 1):
            if (i + j) % 2 == 0:
                tex[j * sq_px:(j + 1) * sq_px, i * sq_px:(i + 1) * sq_px] = 0
    S = square / sq_px                             # 纹理像素 -> 米
    rng = np.random.default_rng(seed)

    with tempfile.TemporaryDirectory() as td:
        made = 0
        while made < n_views:
            # 随机姿态：绕 x/y 倾斜 + 全画面平移，保证覆盖四角
            rx, ry, rz = (rng.uniform(-0.6, 0.6) for _ in range(3))
            R, _ = cv2.Rodrigues(np.array([rx, ry, rz]))
            t = np.array([rng.uniform(-0.12, 0.12), rng.uniform(-0.07, 0.07),
                          rng.uniform(0.28, 0.55)])
            H = K_true @ np.column_stack([R[:, 0], R[:, 1], t]) @ np.diag([S, S, 1.0])
            if abs(H[2, 2]) < 1e-9:
                continue
            img = cv2.warpPerspective(tex, H / H[2, 2], (w, h),
                                      flags=cv2.INTER_LINEAR, borderValue=255)
            # 加畸变（用真实畸变模型把「已畸变」的图再算一次，模拟真实相机）
            if dist:
                img = cv2.undistort(img, K_true, -D_true, None, K_true)
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            img = cv2.GaussianBlur(img, (3, 3), 0.8)
            img = np.clip(img.astype(np.int16) + rng.integers(-6, 7, img.shape), 0, 255).astype(np.uint8)
            cv2.imwrite(os.path.join(td, f"{made:03d}.png"), img)
            made += 1

        print(f"[selftest] 合成 {made} 张（K_true: fx={K_true[0, 0]}, fy={K_true[1, 1]}, "
              f"cx={K_true[0, 2]}, cy={K_true[1, 2]}, 畸变={'有' if dist else '无'}）")
        res = calibrate(frames_from_dir(td, 0), pattern, square)
        vals = report(res)

    err_fx = abs(vals[0] - K_true[0, 0])
    err_fy = abs(vals[1] - K_true[1, 1])
    ok = (err_fx / K_true[0, 0] < 0.01 and err_fy / K_true[1, 1] < 0.01
          and res["rms"] < 0.6)
    print(f"\n[selftest] fx 误差 {err_fx:.3f}px, fy 误差 {err_fy:.3f}px → "
          f"{'PASS ✅ 脚本能还原已知内参' if ok else 'FAIL ❌'}")
    return ok


# --------------------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="棋盘格实拍标定 → ORB-SLAM3 内参（含合成自检）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    src = p.add_argument_group("输入")
    src.add_argument("--frames", help="已抽好的帧目录")
    src.add_argument("--video", help="视频文件（内部抽帧）")
    src.add_argument("--every", type=int, default=5, help="视频每 N 帧取一张（默认 5）")
    src.add_argument("--limit", type=int, default=60, help="最多用多少张（默认 60）")
    p.add_argument("--pattern", default="8x5", help="内角点数 CxR（9x6 格的棋盘 = 8x5，默认 8x5）")
    p.add_argument("--square", type=float, default=25.0, help="格边长，单位 mm（默认 25）")
    p.add_argument("--expect", help="出厂标定换算值 fx,fy,cx,cy，用于对比")
    p.add_argument("--patch", metavar="YAML", help="把标定结果写回 YAML")
    p.add_argument("--selftest", action="store_true", help="合成数据自检后退出")
    a = p.parse_args()

    if a.selftest:
        sys.exit(0 if selftest() else 1)

    if not a.frames and not a.video:
        p.error("需要 --frames 或 --video（或 --selftest）")

    m = re.match(r"^\s*(\d+)\s*[xX,]\s*(\d+)\s*$", a.pattern)
    if not m:
        p.error("--pattern 形如 8x5（内角点数）")
    pattern = (int(m.group(1)), int(m.group(2)))

    views = frames_from_dir(a.frames, a.limit) if a.frames else frames_from_video(a.video, a.every, a.limit)
    print(f"[calib] 棋盘内角点 {pattern[0]}x{pattern[1]}，格边长 {a.square}mm")
    res = calibrate(views, pattern, a.square / 1000.0)
    vals = report(res, a.expect)
    if a.patch:
        patch_yaml(a.patch, vals)


if __name__ == "__main__":
    main()
