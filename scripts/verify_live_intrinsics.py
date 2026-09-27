#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_live_intrinsics.py —— 一条命令验证「手机 → 帧 → YAML 内参」这条链路仍然自洽。

为什么需要它：
  静态特性（LENS_INTRINSIC_CALIBRATION）只描述传感器全像素阵列。
  从传感器到 1280x720 帧，真实经过的裁剪只有**正在推流的那个 session** 才知道，
  而且这是**每帧**都可能变的东西（改分辨率、开防抖、变焦、切镜头都会变）。
  所以「内参对不对」必须对着活体 session 验，而不是对着手册猜。

关键坑（本脚本存在的理由）：
  dumpsys 里同一帧有两条 crop 字段，值**不一样**，含义完全不同：
    android.scaler.cropRegion    = App 请求的区域（IP Webcam 给的是全画幅 4096x3072）
    android.scaler.rawCropRegion = 硬件真正读出的区域（QTI 会按输出宽高比居中裁剪）
  只看 cropRegion 会得出「4:3 被硬拉成 16:9，fx≠fy 差 33%」的错误结论。
  拿 rawCropRegion 算才是对的（设备 croppingType=CENTER_ONLY 时它一定是输出宽高比）。

用法（IP Webcam 必须在推流）：
  python3 scripts/verify_live_intrinsics.py                       # 默认验 photo_slam_config/phone_1280x720.yaml
  python3 scripts/verify_live_intrinsics.py --yaml <file.yaml> --hal 0 --package com.pas.webcam
  退出码 0 = 自洽，1 = 有人动了设置/换了机器/该更新 YAML 了
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import android_intrinsics as AI  # noqa: E402

ADB_SH = os.path.join(HERE, "adb.sh")
LIVE_DUMP = os.path.join(ROOT, "tools", "_captures", "camera_dump_live.txt")


def adb(*args: str) -> str:
    r = subprocess.run(["bash", ADB_SH, *args], capture_output=True, text=True, timeout=180)
    if r.returncode != 0 and not r.stdout:
        raise SystemExit(f"adb {' '.join(args)} 失败：{(r.stderr or '').strip()[:400]}")
    return r.stdout


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """从 JPEG 里读宽高（不依赖 PIL）。"""
    j = 2
    while j < len(data) - 9:
        if data[j] != 0xFF:
            j += 1
            continue
        m = data[j + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3):
            h = (data[j + 5] << 8) | data[j + 6]
            w = (data[j + 7] << 8) | data[j + 8]
            return w, h
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            j += 2
            continue
        j += 2 + ((data[j + 2] << 8) | data[j + 3])
    return None


def first_after(text: str, key: str) -> list[int] | None:
    """取 `key (tag): type[N]` 下一行 `[...]` 的值。"""
    m = re.search(re.escape(key) + r"\s*\([0-9a-fA-F]+\)\s*:\s*[A-Za-z0-9_]+\[\s*\d+\s*\]\s*\n\s*\[([^\]]*)\]",
                  text)
    return [int(float(x)) for x in AI.nums(m.group(1))] if m else None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--yaml", default=os.path.join(ROOT, "photo_slam_config", "phone_1280x720.yaml"))
    p.add_argument("--hal", type=int, default=0, help="HAL 静态块编号（0=后置主摄）")
    p.add_argument("--package", default="com.pas.webcam", help="期望占用相机的 App")
    p.add_argument("--rtsp", action="store_true", help="额外用 ffprobe 验 RTSP 流（需要 ffprobe）")
    a = p.parse_args()

    devices = adb("devices").strip().splitlines()
    if len(devices) < 2:
        print("❌ 没看到 USB 设备：bash scripts/adb.sh devices -l")
        return 1

    print("[1/5] 抓活体 dumpsys（IP Webcam 要在推流）")
    dump = adb("shell", "dumpsys", "media.camera")
    if "CameraDeviceClient" not in dump:
        print(f"❌ dump 里没有活跃 session —— {a.package} 没在推流？")
        return 1
    os.makedirs(os.path.dirname(LIVE_DUMP), exist_ok=True)
    with open(LIVE_DUMP, "w", encoding="utf-8") as fh:
        fh.write(dump)
    print(f"      已存 {os.path.relpath(LIVE_DUMP, ROOT)}（{len(dump) // 1024}KB，可留作内参出处证据）")

    # 注意：split_static_blocks / parse_dump 收的是【路径】；dump 文本要先落盘。
    # （曾经误把 4MB 文本当路径传进去，FileNotFoundError 的信息里会把整份 dump 打出来）
    blocks = AI.split_static_blocks(LIVE_DUMP)
    static = AI.parse_dump_text(blocks[a.hal][1]) if len(blocks) > a.hal else {}
    k = static.get("intrinsic")
    if not k:
        print(f"❌ HAL {a.hal} 没有出厂标定")
        return 1

    print("[2/5] 活跃 session 与流配置")
    pkg = re.search(r"Client package: (\S+)", dump)
    print(f"      占用相机的 App: {pkg.group(1) if pkg else '未知'}"
          f"（期望 {a.package}）{'✅' if pkg and pkg.group(1) == a.package else '⚠️'}")
    dims = re.findall(r"Dims:\s*(\d+)\s*x\s*(\d+),\s*format\s*(0x[0-9a-fA-F]+)", dump)
    dims = [(int(w), int(h), f) for w, h, f in dims][:3]
    for w, h, f in dims:
        kind = {"0x7fa30c06": "PRIVATE(预览)", "0x23": "YUV_420_888",
                "0x21": "JPEG", "0x22": "BLOB"}.get(f, f)
        print(f"      流 {w}x{h}  {kind}")

    print("[3/5] 实际拿到的帧尺寸")
    adb("forward", "tcp:18443", "tcp:8080")
    shot, shot_size = None, None
    try:
        shot = urllib.request.urlopen("http://127.0.0.1:18443/shot.jpg", timeout=10).read()
        shot_size = jpeg_size(shot)
        print(f"      HTTP /shot.jpg = {shot_size[0]}x{shot_size[1]}" if shot_size else "      取不到 /shot.jpg")
    except Exception as e:
        print(f"      ⚠️ 抓不到 HTTP 帧：{e}")
    if a.rtsp:
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                                  "-timeout", "8000000", "-analyzeduration", "4000000",
                                  "-probesize", "2000000",
                                  "-show_entries", "stream=codec_name,width,height",
                                  "-of", "default=nw=1",
                                  "rtsp://127.0.0.1:18443/h264_ulaw.sdp"],
                                 capture_output=True, text=True, timeout=60).stdout
            print("      RTSP: " + " ".join(out.split()))
        except FileNotFoundError:
            print("      （没装 ffprobe，跳过 RTSP 检查）")

    print("[4/5] 逐帧裁剪（这步最关键）")
    crop = first_after(dump, "android.scaler.cropRegion")
    raw = first_after(dump, "android.scaler.rawCropRegion")
    print(f"      cropRegion（App 请求）   = {crop}  = 全画幅，别直接拿它算")
    if not raw or len(raw) != 4:
        print("      rawCropRegion（硬件实读） = 缺失——这台 HAL 不报，得靠棋盘格标定")
        return 1

    # rawCropRegion 的字段约定厂商之间不统一，别猜——用「宽高比必须等于输出宽高比」自证：
    #   (x, y, w, h) 是 QTI vendor 惯例；Rect(L,T,R,B) 是 AOSP 标准惯例。
    # 本机 [0 384 4096 2304]：前者 = 4096x2304 = 16:9 且正好居中 ✅；
    #                        后者 = 4096x1920 = 2.13:1，没有任何流是这个比例 ❌。
    target_ar = (shot_size[0] / shot_size[1]) if shot_size else (dims[0][0] / dims[0][1] if dims else 16 / 9)
    cands = {
        "xywh": (raw[0], raw[1], raw[2], raw[3]),
        "ltrb": (raw[0], raw[1], raw[2] - raw[0], raw[3] - raw[1]),
    }
    scored = [(abs(w / h - target_ar), name, (l, t, w, h)) for name, (l, t, w, h) in cands.items() if h > 0]
    scored.sort()
    err, conv, (cl, ct, cw, ch) = scored[0]
    print(f"      rawCropRegion（硬件实读） = {raw}  → 按 {conv} 解读为 "
          f"x={cl} y={ct} {cw}x{ch}（宽高比 {cw / ch:.4f}，目标 {target_ar:.4f}，偏差 {err * 100:.2f}%）")
    if err > 0.01:
        print("      ⚠️ 两种解读都对不上输出宽高比 —— 这台机器的 rawCropRegion 语义需要人工确认，"
              "别拿它算，直接上棋盘格标定")
        return 1
    if len(scored) > 1 and scored[1][0] < 0.01:
        print("      ⚠️ 两种解读都自洽（多半是整幅区域），按 xywh 继续")

    out_w, out_h = (shot_size if shot_size else (cw, ch))

    print("[5/5] 用 rawCropRegion 换算，对比 YAML")
    ns = argparse.Namespace(
        intrinsic=AI._join(k), focal_mm=None, sensor_mm=None, pixel_array=None,
        active_array=AI._join(static.get("active_array")), active_array_whxy=None,
        active_offset=None, crop=f"{cl},{ct},{cl + cw},{ct + ch}", center_crop=None,
        out=f"{out_w},{out_h}", rotate="0", distortion=None,
    )
    got = AI.compute(ns)

    want = {}
    for line in open(a.yaml, encoding="utf-8"):
        m = re.match(r"^Camera1\.(\w+):\s*([-+0-9.eE]+)", line)
        if m:
            want["k_" + m.group(1)] = float(m.group(2))
        m = re.match(r"^Camera\.(width|height):\s*(\d+)", line)
        if m:
            want[m.group(1)] = int(m.group(2))

    bad = 0
    if want.get("width") != out_w or want.get("height") != out_h:
        print(f"      ❌ 尺寸不符：YAML {want.get('width')}x{want.get('height')} vs 实际 {out_w}x{out_h}")
        bad += 1
    for key, val in (("fx", got.fx), ("fy", got.fy), ("cx", got.cx), ("cy", got.cy)):
        wv = want.get("k_" + key)
        if wv is None:
            print(f"      ⚠️ YAML 里没有 Camera1.{key}")
            bad += 1
            continue
        d = (wv - val) / val * 100
        flag = "✅" if abs(d) < 0.5 else "❌"
        print(f"      {flag} Camera1.{key}: YAML {wv:9.3f}  实算 {val:9.3f}  差 {d:+6.3f}%")
        if abs(d) >= 0.5:
            bad += 1

    print(f"\n{'✅ PASS —— 链路自洽，YAML 与活体 session 一致' if not bad else f'❌ FAIL（{bad} 项不符）'}")
    print(f"   复现：python3 scripts/android_intrinsics.py --dump {os.path.relpath(LIVE_DUMP, ROOT)} "
          f"--hal {a.hal} --crop-ltwh {cl},{ct},{cw},{ch} --out {out_w},{out_h} --rotate 0")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
