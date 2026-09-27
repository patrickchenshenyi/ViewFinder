#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动态 3D Gaussian Splatting demo: 规范空间 + 变形场 (Canonical + Deformation)
=============================================================================

对应"4D 高斯"里最主流的一类改造: 保持一个【静止的规范空间 3DGS】,
再用一个【变形网络 F(x, t)】描述"时间 t 时每个高斯偏移/缩放到哪"。

本 demo 用纯 NumPy 手写一个微型 MLP 作为变形场, 并用梯度下降(Adam)
让它"学会"两个物体的真实运动。核心链路:

    规范空间高斯  --F(mu, t)-->  (Δmu, Δscale)  -->  变形后的高斯  -->  投影+混合渲染

同时渲染三个对照, 直观展示"为什么要加时间维":
    1. 规范空间(无变形)
    2. 用【学习到的变形场】渲染 t 时刻
    3. 【不加时间维】的后果: 运动轨迹被平均成模糊/拖影(鬼影)
    4. 学习到的轨迹 vs 真实轨迹

运行:
    .venv/bin/python dynamic_gaussian_demo.py

输出:
    output_dynamic_demo.png   -- 2x2 对照大图
    output_dynamic.gif        -- 动态场景随时间变化的动画
"""

import os
from dataclasses import dataclass
from pathlib import Path

_here = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(_here / ".mplcache"))

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image

# 复用静态 demo 里的相机 / 投影 / 渲染 / 中文字体
import gaussian_splatting_demo as gs


# ----------------------------------------------------------------------------
# 1. 动态高斯: 每个物体有"规范空间"参数 + 一段真实运动(作为训练监督)
# ----------------------------------------------------------------------------
@dataclass
class DynGaussian:
    name: str
    mu_c: np.ndarray      # 规范中心(运动轨道中心)
    scales_c: np.ndarray  # 规范尺度(标准差)
    rot: np.ndarray       # 3x3 旋转(这里保持单位阵, 演示不涉及旋转)
    alpha: float
    color: np.ndarray
    is_static: bool
    # --- 真实运动(ground truth, 用于监督变形网络) ---
    amp: np.ndarray       # 各轴运动幅度 (3,)
    freq: float           # 频率
    phase: float          # 相位
    scale_amp: float      # 尺度"呼吸"幅度


def gt_offset(g, t):
    """真实位移 Δmu(t)。两个动态物体运动规律不同(网络必须靠位置区分它们)。"""
    if g.is_static:
        return np.zeros(3)
    th = 2.0 * np.pi * g.freq * t + g.phase
    if g.name.startswith("红"):
        # 红色: 在 X-Y 平面做圆形轨道
        return np.array([g.amp[0] * np.cos(th), g.amp[1] * np.sin(th), 0.0])
    # 绿色: 沿 X 大幅摆动 + Y/Z 小扰动(与红色相位相反)
    return np.array([g.amp[0] * np.sin(th), g.amp[1] * np.cos(th), g.amp[2] * np.sin(th)])


def gt_scales(g, t):
    """真实尺度: 周期性"呼吸"。"""
    if g.is_static:
        return g.scales_c.copy()
    th = 2.0 * np.pi * g.freq * t + g.phase
    return g.scales_c * (1.0 + g.scale_amp * np.sin(th))


def build_dynamic_scene():
    """规范空间场景: 1 个静态背景墙 + 2 个运动的椭球。"""
    I = np.eye(3)
    return [
        DynGaussian("墙(静态)", np.array([0.0, 0.0, 8.0]),
                    np.array([3.5, 3.5, 0.3]), I, 0.5,
                    np.array([0.25, 0.50, 0.95]), True,
                    np.zeros(3), 1.0, 0.0, 0.0),
        DynGaussian("红(圆轨道)", np.array([0.0, 0.0, 3.0]),
                    np.array([0.32, 0.32, 0.32]), I, 0.9,
                    np.array([0.90, 0.10, 0.10]), False,
                    np.array([0.85, 0.85, 0.0]), 1.0, 0.0, 0.25),
        DynGaussian("绿(横向摆动)", np.array([-0.6, 0.4, 2.2]),
                    np.array([0.28, 0.28, 0.28]), I, 0.85,
                    np.array([0.10, 0.80, 0.20]), False,
                    np.array([0.70, 0.25, 0.10]), 1.0, np.pi, 0.40),
    ]


def to_gs_gaussian(g, mu, scales):
    """由 (中心, 尺度) 构造渲染所需的 3DGS 高斯(cov = R diag(s^2) R^T)。"""
    cov = g.rot @ np.diag(scales ** 2) @ g.rot.T
    return gs.Gaussian(g.name, mu, cov, g.alpha, g.color)


# ----------------------------------------------------------------------------
# 2. 变形场: 纯 NumPy 微型 MLP  F(x, t) -> (Δmu, Δlog_scale)
# ----------------------------------------------------------------------------
class DeformationMLP:
    def __init__(self, in_dim=4, hidden=48, out_dim=6, seed=0):
        rng = np.random.default_rng(seed)
        self.W1 = rng.normal(0, 0.5, (hidden, in_dim))
        self.b1 = np.zeros(hidden)
        self.W2 = rng.normal(0, 0.5, (hidden, hidden))
        self.b2 = np.zeros(hidden)
        self.W3 = rng.normal(0, 0.5, (out_dim, hidden))
        self.b3 = np.zeros(out_dim)

    def forward(self, X):
        self.z1 = X @ self.W1.T + self.b1
        self.a1 = np.tanh(self.z1)
        self.z2 = self.a1 @ self.W2.T + self.b2
        self.a2 = np.tanh(self.z2)
        self.out = self.a2 @ self.W3.T + self.b3
        return self.out

    def backward(self, X, dout):
        N = X.shape[0]
        dW3 = dout.T @ self.a2 / N
        db3 = dout.mean(axis=0)
        da2 = dout @ self.W3
        dz2 = da2 * (1.0 - self.a2 ** 2)
        dW2 = dz2.T @ self.a1 / N
        db2 = dz2.mean(axis=0)
        da1 = dz2 @ self.W2
        dz1 = da1 * (1.0 - self.a1 ** 2)
        dW1 = dz1.T @ X / N
        db1 = dz1.mean(axis=0)
        return {"W1": dW1, "b1": db1, "W2": dW2, "b2": db2, "W3": dW3, "b3": db3}

    def params(self):
        return {"W1": self.W1, "b1": self.b1, "W2": self.W2,
                "b2": self.b2, "W3": self.W3, "b3": self.b3}

    def predict(self, x, t):
        """单点前向: 输入 (x,y,z,t) -> (Δmu, Δlog_scale)。x 已归一化。"""
        out = self.forward(np.array([[*x, t]]))[0]
        return out[:3], out[3:]


class Adam:
    def __init__(self, lr=1e-3):
        self.lr, self.t = lr, 0
        self.m, self.v = {}, {}

    def step(self, params, grads):
        self.t += 1
        for k in params:
            self.m.setdefault(k, np.zeros_like(params[k]))
            self.v.setdefault(k, np.zeros_like(params[k]))
            self.m[k] = 0.9 * self.m[k] + 0.1 * grads[k]
            self.v[k] = 0.999 * self.v[k] + 0.001 * grads[k] ** 2
            mh = self.m[k] / (1 - 0.9 ** self.t)
            vh = self.v[k] / (1 - 0.999 ** self.t)
            params[k] -= self.lr * mh / (np.sqrt(vh) + 1e-8)


POS_SCALE = 4.0  # 位置输入归一化系数


def make_training_data(scene, n_t=64):
    """构造 (输入, 目标) 训练集: 每个物体在 n_t 个时刻的真实变形。"""
    Xs, Ys = [], []
    for g in scene:
        for t in np.linspace(0.0, 1.0, n_t):
            off = gt_offset(g, t)
            ds_log = np.log(gt_scales(g, t) / g.scales_c)
            Xs.append([* (g.mu_c / POS_SCALE), t])
            Ys.append(np.concatenate([off, ds_log]))
    return np.array(Xs), np.array(Ys)


def train(mlp, X, Y, iters=5000, lr=1e-3):
    opt = Adam(lr)
    for it in range(iters):
        pred = mlp.forward(X)
        loss = np.mean((pred - Y) ** 2)
        dout = 2.0 * (pred - Y) / X.shape[0]
        grads = mlp.backward(X, dout)
        opt.step(mlp.params(), grads)
        if it == 0:
            print(f"  初始 loss = {loss:.4f}")
    print(f"  最终 loss = {loss:.4f}")
    return mlp


# ----------------------------------------------------------------------------
# 3. 用变形场渲染
# ----------------------------------------------------------------------------
CAM = gs.Camera()
W = H = 480


def render_at(scene, mlp, t, use_deform=True):
    """渲染 t 时刻: 每个高斯 -> 变形 -> 构造 3DGS 高斯 -> 投影+混合。"""
    gaussians = []
    for g in scene:
        if use_deform:
            dmu, ds_log = mlp.predict(g.mu_c / POS_SCALE, t)
            mu = g.mu_c + dmu
            scales = g.scales_c * np.exp(ds_log)
        else:
            mu, scales = g.mu_c.copy(), g.scales_c.copy()
        gaussians.append(to_gs_gaussian(g, mu, scales))
    img, _ = gs.render(gaussians, CAM, W, H, mode="persp")
    return img


def blurred_static(scene):
    """
    "不加时间维"的近似: 静态 3DGS 被迫用【一个】高斯去拟合整个运动轨迹,
    结果就是位置取平均 + 协方差沿轨迹拉伸 -> 模糊/拖影。
    """
    gaussians = []
    for g in scene:
        if g.is_static:
            gaussians.append(to_gs_gaussian(g, g.mu_c, g.scales_c))
            continue
        samples = np.array([gt_offset(g, t) for t in np.linspace(0, 1, 64)])
        traj_cov = np.cov(samples.T) + 1e-6 * np.eye(3)
        mu = g.mu_c + samples.mean(axis=0)
        cov = g.rot @ np.diag(g.scales_c ** 2) @ g.rot.T + traj_cov
        gaussians.append(gs.Gaussian(g.name, mu, cov, g.alpha, g.color))
    img, _ = gs.render(gaussians, CAM, W, H, mode="persp")
    return img


# ----------------------------------------------------------------------------
# 4. 可视化 + 动画
# ----------------------------------------------------------------------------
def main():
    font = gs.setup_cjk_font()
    print(f"CJK 字体: {font}")

    scene = build_dynamic_scene()
    X, Y = make_training_data(scene)

    print("训练变形网络 F(x, t) -> (Δmu, Δlog_scale) ...")
    mlp = train(DeformationMLP(), X, Y)

    # 四个面板
    fig, axes = plt.subplots(2, 2, figsize=(12, 12))
    axes[0, 0].imshow(render_at(scene, mlp, 0.0, use_deform=False))
    axes[0, 0].set_title('规范空间(无变形): 静止的 3DGS')
    axes[0, 0].axis('off')

    axes[0, 1].imshow(render_at(scene, mlp, 0.25, use_deform=True))
    axes[0, 1].set_title('t=0.25 时刻: 用【学习到的变形场】渲染')
    axes[0, 1].axis('off')

    axes[1, 0].imshow(blurred_static(scene))
    axes[1, 0].set_title('不加时间维的后果: 轨迹被平均成拖影(鬼影)')
    axes[1, 0].axis('off')

    # 轨迹对比
    ts = np.linspace(0, 1, 80)
    ax = axes[1, 1]
    for g in scene:
        if g.is_static:
            continue
        gt = np.array([gt_offset(g, t) for t in ts])
        pred = np.array([mlp.predict(g.mu_c / POS_SCALE, t)[0] for t in ts])
        c = g.color
        ax.plot(ts, gt[:, 0], color=c, lw=2, label=f"{g.name} GT·x")
        ax.plot(ts, pred[:, 0], color=c, lw=1, ls='--', label=f"{g.name} 学习·x")
        ax.plot(ts, gt[:, 1], color=c, lw=2, alpha=0.4, label=f"{g.name} GT·y")
        ax.plot(ts, pred[:, 1], color=c, lw=1, ls='--', alpha=0.4, label=f"{g.name} 学习·y")
    ax.set_xlabel('时间 t'); ax.set_ylabel('位移 Δμ')
    ax.set_title('学习到的运动轨迹 vs 真实轨迹')
    ax.legend(fontsize=7)

    fig.suptitle("动态 3DGS = 规范空间 + 变形场 F(x,t)", fontsize=14)
    fig.tight_layout()
    fig.savefig(_here / "output_dynamic_demo.png", dpi=110)
    plt.close(fig)

    # GIF 动画
    frames = []
    for t in np.linspace(0, 1, 24):
        img = render_at(scene, mlp, t, use_deform=True)
        frames.append(Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8)))
    frames[0].save(_here / "output_dynamic.gif", save_all=True,
                   append_images=frames[1:], duration=100, loop=0)

    print(f"\n已保存: {_here / 'output_dynamic_demo.png'}")
    print(f"已保存: {_here / 'output_dynamic.gif'}")


if __name__ == "__main__":
    main()
