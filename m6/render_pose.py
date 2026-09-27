#!/usr/bin/env python3
"""M6.0b: headless 3DGS 渲染器 —— 加载 Photo-SLAM 的 ply，给定相机位姿渲染出 2D 图。

复用云端 MonoGS 的 gaussian_splatting（diff-gaussian-rasterization）。
约定（与 Photo-SLAM gaussian_keyframe.cpp 逐条对齐，见 docs/plan.md M6）：
  - 位姿：R_c2w（相机->世界）、C（相机中心，世界系）；
  - w2c = [[R_w2c, t_w2c]]，其中 R_w2c = R_c2w^T，t_w2c = -R_w2c @ C；
  - 投影：centered FoV（focal2fov），z_sign=1，与 Photo-SLAM 的 getProjectionMatrix 同款。

用法：
  LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
  python render_pose.py --ply <ply> --cameras <cameras.json> --id 0 --out out.png

  # 或显式给位姿（quaternion wxyz）+ 内参：
  python render_pose.py --ply <ply> --pose "x y z qx qy qz qw" \
      --fx 892.4 --fy 892.253 --w 1280 --h 720 --out out.png
"""
import argparse
import json
import math
from types import SimpleNamespace

import numpy as np
import torch

MONOGS = "/root/MonoGS"
import sys

sys.path.insert(0, MONOGS)
from gaussian_splatting.scene.gaussian_model import GaussianModel
from gaussian_splatting.gaussian_renderer import render


def load_model(ply_path, sh_degree=3):
    model = GaussianModel(sh_degree=sh_degree)
    model.load_ply(ply_path)
    return model


def projection_matrix(znear, zfar, fovX, fovY):
    """与 Photo-SLAM gaussian_keyframe.cpp getProjectionMatrix 同款（z_sign=1）。"""
    tanHalfFovY = math.tan(fovY / 2)
    tanHalfFovX = math.tan(fovX / 2)
    top = tanHalfFovY * znear
    bottom = -top
    right = tanHalfFovX * znear
    left = -right
    P = np.zeros((4, 4), dtype=np.float32)
    P[0, 0] = 2.0 * znear / (right - left)
    P[1, 1] = 2.0 * znear / (top - bottom)
    P[0, 2] = (right + left) / (right - left)
    P[1, 2] = (top + bottom) / (top - bottom)
    P[3, 2] = 1.0
    P[2, 2] = zfar / (zfar - znear)
    P[2, 3] = -(zfar * znear) / (zfar - znear)
    return P


def build_camera(R_c2w, C, fx, fy, w, h, znear=0.01, zfar=100.0):
    """构造 MonoGS render() 需要的 viewpoint_camera（SimpleNamespace）。"""
    R_c2w = np.asarray(R_c2w, dtype=np.float32).reshape(3, 3)
    C = np.asarray(C, dtype=np.float32).reshape(3)
    R_w2c = R_c2w.T
    t_w2c = -R_w2c @ C

    W2C = np.eye(4, dtype=np.float32)
    W2C[:3, :3] = R_w2c
    W2C[:3, 3] = t_w2c

    FoVx = 2.0 * math.atan(w / (2.0 * fx))
    FoVy = 2.0 * math.atan(h / (2.0 * fy))
    P = projection_matrix(znear, zfar, FoVx, FoVy)

    dev = "cuda"
    # 关键：CUDA 光栅器按列主序(glm)读矩阵，故与 3DGS/MonoGS Camera 一样先转置
    w2c_t = torch.tensor(W2C, dtype=torch.float32, device=dev).transpose(0, 1)
    proj_t = torch.tensor(P, dtype=torch.float32, device=dev).transpose(0, 1)
    full_proj = w2c_t @ proj_t  # == (P @ W2C)^T，与 MonoGS Camera.full_proj_transform 一致

    cam = SimpleNamespace(
        image_width=int(w),
        image_height=int(h),
        FoVx=FoVx,
        FoVy=FoVy,
        world_view_transform=w2c_t,
        projection_matrix=proj_t,
        full_proj_transform=full_proj,
        camera_center=torch.tensor(C, dtype=torch.float32, device=dev),
        # MonoGS 魔改 rasterizer 需要这两个 pose-delta（静态渲染置 0）
        cam_rot_delta=torch.zeros(3, dtype=torch.float32, device=dev),
        cam_trans_delta=torch.zeros(3, dtype=torch.float32, device=dev),
    )
    return cam


def render_pose(model, cam, bg=(0.0, 0.0, 0.0)):
    """渲染并返回 HxWx3 uint8 numpy 数组。"""
    bg_color = torch.tensor(bg, dtype=torch.float32, device="cuda")
    pipe = SimpleNamespace(convert_SHs_python=False, compute_cov3D_python=False)
    with torch.no_grad():
        out = render(cam, model, pipe, bg_color, scaling_modifier=1.0)
    img = out["render"].permute(1, 2, 0).detach().cpu().numpy()
    img = np.clip(img, 0.0, 1.0)
    return (img * 255.0).astype(np.uint8)


def pose_from_quat(x, y, z, qx, qy, qz, qw):
    """C = (x,y,z), R_c2w = quaternion(w,x,y,z)->3x3。"""
    R = torch.tensor(
        [[1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
         [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
         [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]],
        dtype=torch.float32,
    ).numpy()
    return R, np.array([x, y, z], dtype=np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", default=None, help="cameras.json 路径（按 --id 取位姿）")
    ap.add_argument("--id", type=int, default=0)
    ap.add_argument("--pose", default=None, help="显式位姿 'x y z qx qy qz qw'")
    ap.add_argument("--fx", type=float, default=None)
    ap.add_argument("--fy", type=float, default=None)
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--sh", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    model = load_model(args.ply, sh_degree=args.sh)
    print(f"[render] loaded {args.ply}", file=sys.stderr)

    if args.pose:
        p = [float(v) for v in args.pose.split()]
        assert len(p) == 7
        R, C = pose_from_quat(*p)
        fx = args.fx or (args.w / 2)
        fy = args.fy or (args.h / 2)
    else:
        with open(args.cameras) as f:
            cams = json.load(f)
        cam_meta = None
        for c in cams:
            if c["id"] == args.id:
                cam_meta = c
                break
        if cam_meta is None:
            ids = sorted(c["id"] for c in cams)
            raise SystemExit(
                f"[error] cameras.json 中找不到 id={args.id}"
                f"（ids {ids[0]}..{ids[-1]}，共 {len(ids)} 个，非稠密）")
        R = np.asarray(cam_meta["rotation"], dtype=np.float32)
        C = np.asarray(cam_meta["position"], dtype=np.float32)
        fx = args.fx if args.fx is not None else cam_meta["fx"]
        fy = args.fy if args.fy is not None else cam_meta["fy"]
        args.w = cam_meta["width"]
        args.h = cam_meta["height"]

    cam = build_camera(R, C, fx, fy, args.w, args.h)
    img = render_pose(model, cam)
    from PIL import Image

    Image.fromarray(img).save(args.out)
    print(f"[render] saved {args.out} ({img.shape[1]}x{img.shape[0]})", file=sys.stderr)


if __name__ == "__main__":
    main()
