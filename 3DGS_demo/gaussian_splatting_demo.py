#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
3D Gaussian Splatting 核心链路 demo
===================================

用纯 NumPy 实现并可视化 3DGS 渲染的完整管线:

    3D 高斯(椭球)  -->  透视投影(EWA 一阶近似/Jacobian)  -->  按深度排序  -->  alpha 混合

同时额外渲染一个「正交投影」版本作对比, 直观印证:
    * 相机成像是「从光心出发的透视投影」, 不是「垂直投影到像平面」。
    * 3D 高斯经透视投影后不是严格 2D 高斯, 用 Jacobian 做一阶近似(EWA splatting)。
    * 排序按「沿光轴的深度」从近到远, 再做 alpha 混合。

运行:
    .venv/bin/python gaussian_splatting_demo.py

输出:
    output_3dgs_demo.png    -- 2x2 可视化大图
    output_render.png       -- 最终渲染出的"图像"
"""

import os
from dataclasses import dataclass
from pathlib import Path

# 让 matplotlib 把缓存写到工作目录, 避免对用户主目录的写权限问题
_here = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(_here / ".mplcache"))

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import cm
from matplotlib import font_manager

# 数值稳定项: 真实 3DGS 实现里会在 3D 协方差对角加 0.3, 防止病态/除零。
# 本 demo 的合成高斯尺度很小(约 0.2), 加 0.3 会把椭球撑得过大、彼此糊成一团,
# 所以这里用一个很小的值——原理完全一致, 只是量级适配 demo 场景。
EPS_SIGMA = 1e-3


def setup_cjk_font():
    """注册一个系统中文字体, 否则图里的中文会显示成方块。"""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]
    for p in candidates:
        if Path(p).exists():
            try:
                font_manager.fontManager.addfont(p)
                name = font_manager.FontProperties(fname=p).get_name()
                plt.rcParams["font.family"] = [name, "DejaVu Sans"]
                plt.rcParams["axes.unicode_minus"] = False
                return name
            except Exception:
                continue
    return None


# ----------------------------------------------------------------------------
# 1. 数据表示: 一个 3D 高斯
# ----------------------------------------------------------------------------
@dataclass
class Gaussian:
    name: str
    mu: np.ndarray      # (3,) 中心位置
    cov: np.ndarray     # (3,3) 协方差矩阵 -> 决定"椭球"的大小和朝向
    alpha: float        # 不透明度 [0,1]
    color: np.ndarray   # (3,) RGB [0,1]


def rotation_matrix(axis, angle_deg):
    """绕 axis ('x'|'y'|'z') 旋转 angle_deg 度, 返回 3x3 旋转矩阵。"""
    a = np.radians(angle_deg)
    c, s = np.cos(a), np.sin(a)
    if axis == 'x':
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == 'y':
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def make_gaussian(name, mu, scales, alpha, color, rot=None):
    """
    由「中心 + 三轴标准差 + 可选旋转」构造一个高斯。
    cov = R @ diag(scales^2) @ R^T
    """
    R = np.eye(3)
    if rot:
        for axis, deg in rot:
            R = R @ rotation_matrix(axis, deg)
    S2 = np.diag(np.array(scales, dtype=float) ** 2)
    cov = R @ S2 @ R.T
    return Gaussian(name, np.array(mu, float), cov, alpha, np.array(color, float))


def build_scene():
    """构造一个 4 个高斯的小场景, 深度从前到后依次是 黄 < 红 < 绿 < 蓝墙。"""
    gaussians = [
        # 背景墙: 又大又扁的蓝色椭球, 半透明
        make_gaussian("墙(最远)", [0.0, 0.0, 8.0], [3.5, 3.5, 0.3], 0.50,
                      [0.25, 0.50, 0.95]),
        # 中间绿色椭球
        make_gaussian("绿(中间)", [-0.60, 0.30, 3.5], [0.35, 0.30, 0.30], 0.70,
                      [0.10, 0.80, 0.20]),
        # 红色椭球, 比绿色更近, 在投影上部分遮挡绿色
        make_gaussian("红(较近)", [-0.85, -0.15, 2.2], [0.30, 0.28, 0.28], 0.90,
                      [0.90, 0.10, 0.10]),
        # 黄色小椭球, 最靠前
        make_gaussian("黄(最近)", [0.45, 0.30, 1.6], [0.18, 0.15, 0.15], 0.95,
                      [1.00, 0.85, 0.10]),
    ]
    return gaussians


# ----------------------------------------------------------------------------
# 2. 相机 + 投影
# ----------------------------------------------------------------------------
class Camera:
    """针孔相机: 位姿 (R 世界->相机, t 相机中心), 焦距 f, 主点 (cx,cy)。"""

    def __init__(self, R=None, t=None, f=250.0, cx=240.0, cy=240.0):
        self.R = R if R is not None else np.eye(3)
        self.t = t if t is not None else np.zeros(3)
        self.f = f
        self.cx = cx
        self.cy = cy


def project_gaussian(g, cam, mode="persp"):
    """
    把一个 3D 高斯投影到 2D, 返回 (2D 中心, 2D 协方差, 深度)。

    mode = "persp": 透视投影(EWA 一阶近似)
           "ortho": 正交投影(用于对比, 说明透视 != 正交)

    关键数学:
        世界 -> 相机:  X_c = R @ (mu - t)
        透视投影:      u = f * X/Z + cx,  v = f * Y/Z + cy
        Jacobian:      J = [[f/Z, 0, -fX/Z^2],
                            [0, f/Z, -fY/Z^2]]
        2D 协方差:     cov2 = J @ (R cov R^T + 0.3 I) @ J^T
    """
    Xc = cam.R @ (g.mu - cam.t)
    x, y, z = Xc[0], Xc[1], Xc[2]

    if mode == "ortho":
        # 正交投影: 平行光线, 没有 1/z 缩放 -> 这正是很多人对"投影"的直觉误读
        s = cam.f / 4.0          # 随意取个缩放, 便于同框对比
        u, v = s * x + cam.cx, s * y + cam.cy
        J = np.array([[s, 0.0, 0.0], [0.0, s, 0.0]])
    else:
        u, v = cam.f * x / z + cam.cx, cam.f * y / z + cam.cy
        J = np.array([
            [cam.f / z, 0.0, -cam.f * x / (z * z)],
            [0.0, cam.f / z, -cam.f * y / (z * z)],
        ])

    # 世界协方差 -> 相机坐标, 并加对角稳定项(真实实现是 0.3, 见 EPS_SIGMA 注释)
    cov_cam = cam.R @ g.cov @ cam.R.T + EPS_SIGMA * np.eye(3)
    # 一阶近似投影到 2D
    cov2 = J @ cov_cam @ J.T
    cov2 = 0.5 * (cov2 + cov2.T)  # 保证对称

    return np.array([u, v]), cov2, z


# ----------------------------------------------------------------------------
# 3. 渲染: 深度排序 + alpha 混合
# ----------------------------------------------------------------------------
def render(gaussians, cam, W, H, mode="persp", background=(0.02, 0.02, 0.02)):
    """
    把场景渲染成 (H, W, 3) 的图像。
    流程: 投影所有高斯 -> 按深度从近到远排序 -> 逐像素做前向 alpha 混合。

    前向混合公式(transmittance 形式):
        C = 0, T = 1
        for g in sorted_gaussians:      # 从近到远
            C += T * a * color
            T *= (1 - a)
        out = C + T * background
    """
    # 1) 投影
    proj = []
    for g in gaussians:
        mu2, cov2, depth = project_gaussian(g, cam, mode)
        proj.append((depth, g, mu2, cov2))

    # 2) 按深度排序: 相机朝 +Z, 所以 z 越小越近 -> 升序 = 从近到远
    proj.sort(key=lambda p: p[0])

    # 3) 逐像素 alpha 混合
    acc_color = np.zeros((H, W, 3))   # C
    transmittance = np.ones((H, W))   # T
    for depth, g, mu2, cov2 in proj:
        inv_cov = np.linalg.inv(cov2)
        # 由 2D 协方差的特征值求 3-sigma 包围盒(只在该范围内计算, 加速)
        vals, vecs = np.linalg.eigh(cov2)
        stds = np.sqrt(np.maximum(vals, 1e-12))
        r_u = 3.0 * np.sqrt(np.sum((vecs[0, :] * stds) ** 2))
        r_v = 3.0 * np.sqrt(np.sum((vecs[1, :] * stds) ** 2))
        u0 = max(0, int(np.floor(mu2[0] - r_u)))
        u1 = min(W, int(np.ceil(mu2[0] + r_u)) + 1)
        v0 = max(0, int(np.floor(mu2[1] - r_v)))
        v1 = min(H, int(np.ceil(mu2[1] + r_v)) + 1)
        if u1 <= u0 or v1 <= v0:
            continue

        for v in range(v0, v1):
            for u in range(u0, u1):
                d = np.array([u - mu2[0], v - mu2[1]])
                gauss_weight = np.exp(-0.5 * float(d @ inv_cov @ d))
                a = g.alpha * gauss_weight
                if a < 1e-4:
                    continue
                # 前向混合: 当前层贡献 = 尚未被遮挡的光 T * a * color
                acc_color[v, u] += transmittance[v, u] * a * g.color
                transmittance[v, u] *= (1.0 - a)

    bg = np.array(background, float)
    img = acc_color + transmittance[..., None] * bg
    return np.clip(img, 0, 1), proj


# ----------------------------------------------------------------------------
# 4. 可视化
# ----------------------------------------------------------------------------
def ellipsoid_points(mu, cov, n=28):
    """由 (mu, cov) 生成椭球表面点(用于 3D 线框)。"""
    vals, vecs = np.linalg.eigh(cov)
    radii = np.sqrt(np.maximum(vals, 0.0))
    u = np.linspace(0, 2 * np.pi, n)
    v = np.linspace(0, np.pi, n)
    x = np.outer(np.cos(u), np.sin(v))
    y = np.outer(np.sin(u), np.sin(v))
    z = np.outer(np.ones_like(u), np.cos(v))
    p = np.stack([x, y, z]).reshape(3, -1)          # (3, n*n)
    p_t = vecs @ (radii[:, None] * p) + mu[:, None]  # 变换
    return p_t.reshape(3, n, n)


def ellipse_2d(mu2, cov2, n=120, sigma=1.0):
    """由 2D 协方差画 1-sigma 椭圆轮廓。"""
    vals, vecs = np.linalg.eigh(cov2)
    stds = np.sqrt(np.maximum(vals, 0.0))
    th = np.linspace(0, 2 * np.pi, n)
    circle = np.stack([np.cos(th), np.sin(th)])
    pts = vecs @ (stds[:, None] * circle * sigma) + mu2[:, None]
    return pts[0], pts[1]


def draw_scene_3d(ax, gaussians):
    """3D 线框: 椭球 + 相机光心。"""
    ax.scatter([0], [0], [0], c='k', s=60, marker='*', label='相机光心')
    for g in gaussians:
        X, Y, Z = ellipsoid_points(g.mu, g.cov)
        ax.plot_surface(X, Y, Z, color=g.color, alpha=0.45,
                        linewidth=0, antialiased=False)
        ax.plot_wireframe(X[::4, ::4], Y[::4, ::4], Z[::4, ::4],
                          color=g.color, linewidth=0.5, alpha=0.8)
        ax.text(*g.mu, g.name, fontsize=7)
    ax.set_xlabel('X'); ax.set_ylabel('Y'); ax.set_zlabel('Z (光轴/深度)')
    ax.set_title('3D 场景: 椭球体(3D 高斯)')


def draw_scene_2d(ax, proj, order_labels=True):
    """2D 投影后的高斯(1-sigma 椭圆)。"""
    ax.invert_yaxis()  # 图像坐标 y 向下
    for i, (depth, g, mu2, cov2) in enumerate(proj):
        xs, ys = ellipse_2d(mu2, cov2, sigma=1.0)
        ax.fill(xs, ys, color=g.color, alpha=0.35 * g.alpha)
        ax.plot(xs, ys, color=g.color, lw=1.5)
        ax.text(mu2[0], mu2[1], f"#{i+1} {g.name}\nz={depth:.1f}",
                fontsize=6, ha='center', va='center', color='white')
    ax.set_xlim(0, 480); ax.set_ylim(0, 480)
    ax.set_aspect('equal')
    ax.set_title('投影到像平面 + 按深度排序(#1 最近)')


def main():
    font_name = setup_cjk_font()
    print(f"CJK 字体: {font_name}")
    W = H = 480
    gaussians = build_scene()
    cam = Camera()

    # 透视渲染(正确做法)
    img_persp, proj = render(gaussians, cam, W, H, mode="persp")
    # 正交渲染(对比: 常见误读)
    img_ortho, _ = render(gaussians, cam, W, H, mode="ortho")

    # 控制台: 打印排序结果
    print("按深度从近到远的混合顺序:")
    for i, (depth, g, mu2, cov2) in enumerate(proj):
        print(f"  #{i+1}  {g.name:10s}  深度 z={depth:.2f}  alpha={g.alpha:.2f}  "
              f"颜色={g.color}")

    # 画 2x2
    fig, axes = plt.subplots(2, 2, figsize=(12, 12))
    ax3d = fig.add_subplot(2, 2, 1, projection='3d')
    draw_scene_3d(ax3d, gaussians)
    draw_scene_2d(axes[0, 1], proj)
    axes[1, 0].imshow(img_persp); axes[1, 0].set_title('透视投影渲染(正确)')
    axes[1, 0].axis('off')
    axes[1, 1].imshow(img_ortho); axes[1, 1].set_title('正交投影(误读对比)')
    axes[1, 1].axis('off')

    fig.suptitle("3D Gaussian Splatting: 投影 -> 排序 -> alpha 混合", fontsize=14)
    fig.tight_layout()

    out = _here / "output_3dgs_demo.png"
    fig.savefig(out, dpi=110)
    plt.close(fig)

    # 单独保存渲染图
    plt.imsave(_here / "output_render.png", img_persp)

    print(f"\n已保存: {out}")
    print(f"已保存: {_here / 'output_render.png'}")


if __name__ == "__main__":
    main()
