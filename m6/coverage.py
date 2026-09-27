#!/usr/bin/env python3
"""M6.6.2: coverage 可判读性指标（双背景防误判）。

双背景原理：用一对「背景色」各渲染一次，coverage = 在任一背景渲染中与背景差异显著的像素占比。
  - 品红/绿（旧默认）：场景含品红/紫系内容时单品红会误判为空，绿兜底；反之亦然。
  - 黑/白（推荐）：黑渲染可兼作展示图（省一次渲染）；场景含深色内容时黑会误判，白兜底。
  两色在色彩空间相距足够远 → 一个静态场景像素不可能同时接近两者 → 只有真未覆盖像素判空。
structure = 覆盖区内梯度能量（区分「有内容的场景」与「均匀雾」）。
只否决「完全不可判读」：coverage < cov_floor 或 structure < structure_floor。
"""
import numpy as np

import render_pose

MAGENTA = (1.0, 0.0, 1.0)
GREEN = (0.0, 1.0, 0.0)
BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)


def render_bg(model, R, C, fx, fy, w, h, bg):
    cam = render_pose.build_camera(R, C, fx, fy, w, h)
    return render_pose.render_pose(model, cam, bg=bg)


def render_black(model, R, C, fx, fy, w, h):
    return render_bg(model, R, C, fx, fy, w, h, BLACK)


def render_white(model, R, C, fx, fy, w, h):
    return render_bg(model, R, C, fx, fy, w, h, WHITE)


def render_magenta(model, R, C, fx, fy, w, h):  # 兼容旧调用
    return render_bg(model, R, C, fx, fy, w, h, MAGENTA)


def render_green(model, R, C, fx, fy, w, h):  # 兼容旧调用
    return render_bg(model, R, C, fx, fy, w, h, GREEN)


def coverage_mask(img_a, img_b=None, bg_a=MAGENTA, bg_b=GREEN, eps=0.10):
    """覆盖掩膜（bool HxW）：与任一背景差异显著的像素。"""
    fa = img_a.astype(np.float64) / 255.0
    covered = np.abs(fa - np.asarray(bg_a, np.float64)).max(axis=2) > eps
    if img_b is not None:
        fb = img_b.astype(np.float64) / 255.0
        covered = covered | (np.abs(fb - np.asarray(bg_b, np.float64)).max(axis=2) > eps)
    return covered


def coverage_metrics(img_a, img_b=None, bg_a=MAGENTA, bg_b=GREEN, eps=0.10,
                     structure_floor=3.0, cov_floor=0.05):
    """返回 {coverage, structure, judgeable}。

    img_a: 主背景渲染；img_b: 次背景渲染（可选，给了则双背景防误判）。
    coverage: 与任一背景差异显著的像素占比（0-1）；
    structure: 主渲染覆盖区内梯度能量均值（0-255 尺度，区分白雾 vs 内容）。
    """
    cov_a = coverage_mask(img_a, None, bg_a, bg_b, eps)
    covered = coverage_mask(img_a, img_b, bg_a, bg_b, eps)
    coverage = float(covered.mean())
    if cov_a.mean() > 0.01:
        g = img_a.astype(np.float64).mean(axis=2)
        gy, gx = np.gradient(g)
        structure = float((gx * gx + gy * gy)[cov_a].mean())
    else:
        structure = 0.0
    return {"coverage": coverage, "structure": structure,
            "judgeable": bool(coverage >= cov_floor and structure >= structure_floor)}
