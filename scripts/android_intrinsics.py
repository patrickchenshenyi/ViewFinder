#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
android_intrinsics.py —— 把 Android CameraCharacteristics 的静态参数换算成
ORB-SLAM3 / Photo-SLAM 用的针孔内参 (fx, fy, cx, cy, k1, k2, p1, p2, k3)。

两条输入路线：
  A) 直接给数（推荐，最不容易出错）：
     --intrinsic fx,fy,cx,cy[,s]         # LENS_INTRINSIC_CALIBRATION 原值（像素，参考系=全像素阵列）
     或
     --focal-mm 4.25 --sensor-mm 5.76,4.29 --pixel-array 4032,3024   # 由焦距+物理尺寸推导
  B) 解析 adb dump（真机零安装，推荐先走这条）：
     bash scripts/adb.sh shell dumpsys media.camera > tools/_captures/camera_dump.txt
     python3 scripts/android_intrinsics.py --dump tools/_captures/camera_dump.txt --list-cameras
     python3 scripts/android_intrinsics.py --dump tools/_captures/camera_dump.txt --hal 0 \
         --crop-ltwh 0,384,4096,2304 --out 1280,720 --rotate 0
     （--list-cameras 先看这台机器有几颗相机、哪颗有 intrinsicCalibration；
       --hal 选第几块；--crop-ltwh 传该输出流实际的 SCALER_CROP_REGION）

坐标系换算（这是最容易错的一步）：
  全像素阵列 (pixel array) --裁剪--> active array --裁剪--> crop region --> 缩放到输出帧
  本脚本默认认为输出帧 = active array（可被 --crop 覆盖），再做 --rotate 旋转。

用法示例：
  # 已知出厂标定 + 720p 竖屏
  python3 scripts/android_intrinsics.py \
      --intrinsic 3040.5,3040.5,2016,1512 --active-array 0,0,4032,3024 \
      --out 720,1280 --rotate 90

  # 没有出厂标定，用焦距+物理尺寸推导
  python3 scripts/android_intrinsics.py \
      --focal-mm 4.25 --sensor-mm 5.76,4.29 --pixel-array 4032,3024 \
      --active-array 0,0,4032,3024 --out 720,1280 --rotate 90

  # 把结果写回 phone_slam_config/phone_720p.yaml（会先打印 diff）
  python3 scripts/android_intrinsics.py ... --patch photo_slam_config/phone_720p.yaml
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------
# 数值解析小工具
# --------------------------------------------------------------------------------------

NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def nums(text: str) -> list[float]:
    """从 '[...]' / '(a, b, c)' / '4.25' 这类文本里抠出所有数字。"""
    return [float(m.group(0)) for m in NUM_RE.finditer(text)]


def parse_pair(text: str, name: str) -> tuple[float, float]:
    v = nums(text)
    if len(v) < 2:
        raise SystemExit(f"参数 {name} 需要两个数字（如 720,1280），收到：{text!r}")
    return v[0], v[1]


def parse_ratio(text: str, name: str) -> float:
    """解析宽高比：'16:9' / '16,9' / '1.7778' 都行。"""
    v = nums(text)
    if len(v) == 1 and v[0] > 0:
        return v[0]
    if len(v) == 2 and v[1] != 0 and v[0] > 0:
        return v[0] / v[1]
    raise SystemExit(f"参数 {name} 需要 'W:H'（如 16:9）或一个正数，收到：{text!r}")


def parse_rect(text: str, name: str) -> tuple[int, int, int]:
    """解析矩形。接受两种约定，自动判断：

      'L,T,R,B'  (Android Rect，右/下为开区间)
      'W,H,X,Y'  (Size + 左上角偏移)

    当偏移为 0（绝大多数机型的 active array 都是 0,0）时两种解读完全等价。
    返回 (left, top, width, height)。
    """
    v = [int(round(x)) for x in nums(text)]
    if len(v) == 2:
        return 0, 0, v[0], v[1]
    if len(v) != 4:
        raise SystemExit(f"参数 {name} 需要 2 或 4 个整数，收到：{text!r}")
    l, t, a, b = v
    # 判据：第 3/4 个数比第 1/2 个数小，或前两个数比后两个数的一半还大 => 只能是 W,H,X,Y
    if a < l or b < t or l > a / 2 or t > b / 2:
        w, h, x, y = l, t, a, b
        if not (a < l or b < t):
            print(f"[note] {name}={text!r} 按 W,H,X,Y 解读为 w={w} h={h} x={x} y={y}；"
                  f"如果你给的是 Rect(L,T,R,B) 请改用 --active-array 显式写清", file=sys.stderr)
        return x, y, w, h
    return l, t, a - l, b - t


# --------------------------------------------------------------------------------------
# dumpsys media.camera 解析
# --------------------------------------------------------------------------------------

KEY_ALIASES = {
    # 归一化 key（小写、去掉 android. 与下划线）-> 语义名
    "lensintrinsiccalibration": "intrinsic",
    "lensdistortion": "distortion",
    "lensradialdistortion": "radial_distortion",
    "lensinfoavailablefocallengths": "focal_mm",
    "sensorinfophysicalsize": "physical_size_mm",
    "sensorinfopixelarraysize": "pixel_array",
    "sensorinfoactivearraysize": "active_array",
    "sensororientation": "orientation",
    "scalersavailablemaxdigitalzoom": "max_digital_zoom",
    "controlzoomratiorange": "zoom_ratio_range",
    "distortioncorrectionavailablemodes": "distortion_modes",
    "lensposeReference".lower(): "pose_reference",
    "lensposerotation": "pose_rotation",
    "lensposetranslation": "pose_translation",
    "lensinfohyperfocaldistance": "hyperfocal",
    "lensinfominimumfocusdistance": "min_focus",
    "controlvideoStabilizationModes".lower(): "video_stab_modes",
}

DUMP_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.]*)\s*[=:]\s*(.+?)\s*$")

# 真机 `dumpsys media.camera`（Android 11+ / QTI HAL）的静态元数据长这样：
#
#     == Camera HAL device device@1.1/vendor_qti/0 (v1.3) static information: ==
#       API2 camera characteristics:
#         Dumping camera metadata array: 196 / 196 entries, ...
#           android.lens.intrinsicCalibration (8000a): float[5]
#             [2855.68188477 2855.21386719 2025.55200195 1526.74633789 0.00000000 ]
#
# 即「key (tag): type[count]」一行 + 值在【下一行】的 [ ... ]。老解析器只认同行格式，
# 对这种 dump 会一个字段都认不出来（而且会把 'float[5]' 误读成 [5]），所以必须分开处理。
STATIC_BLOCK_RE = re.compile(r"^\s*==\s*Camera HAL device\b.*static information:\s*==\s*$")
TYPED_KEY_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_.]*)\s*\(([0-9a-fA-F]+)\)\s*:\s*[A-Za-z0-9_]+\s*\[\s*\d+\s*\]\s*$"
)
VALUE_ROW_RE = re.compile(r"^\s*\[(.*)\]\s*$")


def _normalize_key(key: str) -> str:
    norm = key.lower()
    for prefix in ("android.", "com.android."):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
    return norm.replace("_", "").replace(".", "")


def split_static_blocks(path: str) -> list[tuple[str, list[str]]]:
    """把 dumpsys 输出按 `== Camera HAL device ... static information: ==` 切块。

    返回 [(块标题, 块内文本行), ...]，顺序 = HAL 设备顺序（通常等同于 camera id 顺序）。
    没有这种分块时返回 []，调用方退回「整文件解析」（兼容老的 dumpsys/自造 dump）。
    """
    blocks: list[tuple[str, list[str]]] = []
    title: str | None = None
    buf: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if STATIC_BLOCK_RE.match(line):
                if title is not None:
                    blocks.append((title, buf))
                title, buf = line.strip().strip("= ").strip(), []
                continue
            if title is not None:
                buf.append(line)
    if title is not None:
        blocks.append((title, buf))
    return blocks


def parse_dump_text(lines) -> dict[str, list[float]]:
    """解析单个静态块（或整份 dump）的文本行。"""
    found: dict[str, list[float]] = {}
    raw: dict[str, str] = {}
    pending: str | None = None          # 已见到 key、正在等下一行的值
    for line in lines:
        m = TYPED_KEY_RE.match(line)
        if m:
            pending = m.group(1)
            raw[pending] = ""
            continue
        if pending is not None:
            mv = VALUE_ROW_RE.match(line)
            if mv:
                raw[pending] = (raw.get(pending, "") + " " + mv.group(1)).strip()
                norm = _normalize_key(pending)
                if norm in KEY_ALIASES:
                    found[KEY_ALIASES[norm]] = nums(raw[pending])
                continue
            # 值行结束（多行 byte[] 也在这里被截断，够用）；空行/新 key 则收尾
            pending = None

        m2 = DUMP_LINE_RE.match(line)
        if not m2:
            continue
        key, val = m2.group(1), m2.group(2)
        raw[key] = val
        norm = _normalize_key(key)
        if norm in KEY_ALIASES:
            found[KEY_ALIASES[norm]] = nums(val)
    found["_raw"] = raw  # type: ignore[assignment]
    return found


def parse_dump(path: str, block: int | None = None) -> dict[str, list[float]]:
    """解析 dump。block=None 时：有分块就取第 0 块（通常是后置主摄）。"""
    blocks = split_static_blocks(path)
    if not blocks:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return parse_dump_text(fh)
    idx = 0 if block is None else block
    if not 0 <= idx < len(blocks):
        raise SystemExit(f"--hal {idx} 越界：该 dump 只有 {len(blocks)} 个 HAL 静态块（0~{len(blocks) - 1}）")
    return parse_dump_text(blocks[idx][1])


# --------------------------------------------------------------------------------------
# 核心换算
# --------------------------------------------------------------------------------------

@dataclass
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    width: float
    height: float
    source: str
    skew: float | None = None
    distortion: list[float] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def hfov_deg(self) -> float:
        return 2.0 * math.degrees(math.atan(self.width / (2.0 * self.fx)))

    @property
    def vfov_deg(self) -> float:
        return 2.0 * math.degrees(math.atan(self.height / (2.0 * self.fy)))


def build_pixel_array_intrinsics(a) -> tuple[float, float, float, float, float | None, str]:
    """返回参考系为【全像素阵列】的 (fx, fy, cx, cy, skew, 来源说明)。"""
    if a.intrinsic:
        v = [float(x) for x in nums(a.intrinsic)]
        if len(v) < 4:
            raise SystemExit("--intrinsic 至少需要 fx,fy,cx,cy 四个数")
        skew = v[4] if len(v) > 4 else None
        return v[0], v[1], v[2], v[3], skew, "LENS_INTRINSIC_CALIBRATION (出厂标定)"

    if a.focal_mm is None:
        raise SystemExit("需要 --intrinsic 或 --focal-mm（可配 --sensor-mm/--pixel-array 推导）")

    f_mm = float(a.focal_mm)
    if a.sensor_mm:
        sw, sh = parse_pair(a.sensor_mm, "--sensor-mm")
    else:
        raise SystemExit("推导焦距需要 --sensor-mm W,H（SENSOR_INFO_PHYSICAL_SIZE，单位 mm）")

    if a.pixel_array:
        pw, ph = parse_pair(a.pixel_array, "--pixel-array")
    elif a.active_array:
        pw, ph = parse_rect(a.active_array, "--active-array")[2:]
        print("[warn] 未给 --pixel-array，退而用 active array 尺寸算像素间距（近似）", file=sys.stderr)
    else:
        raise SystemExit("推导焦距需要 --pixel-array W,H（SENSOR_INFO_PIXEL_ARRAY_SIZE，单位像素）")

    pitch_x = sw / pw          # mm / px
    pitch_y = sh / ph
    fx = f_mm / pitch_x
    fy = f_mm / pitch_y
    cx = pw / 2.0
    cy = ph / 2.0
    return fx, fy, cx, cy, None, f"推导：f={f_mm}mm, 像素间距={pitch_x * 1000:.3f}µm"


def map_to_output(
    fx: float, fy: float, cx: float, cy: float,
    crop: tuple[int, int, int, int],
    out_w: float, out_h: float,
    rotate: int,
    warnings: list[str],
) -> tuple[float, float, float, float]:
    """把【全像素阵列】坐标下的内参映射到输出帧坐标。crop 已换算到 pixel-array 坐标系。"""
    l, t, cw, ch = crop
    if rotate in (90, 270):
        # 旋转后：传感器 x 轴落到输出 v 轴（长度 out_h），传感器 y 轴落到输出 u 轴（长度 out_w）
        sx = out_h / cw
        sy = out_w / ch
    else:
        sx = out_w / cw
        sy = out_h / ch
    # 先平移到 crop 左上角并缩放
    x = (cx - l) * sx
    y = (cy - t) * sy
    fx_s, fy_s = fx * sx, fy * sy

    if abs(sx - sy) > 1e-6:
        warnings.append(
            f"x/y 缩放不一致 (sx={sx:.5f}, sy={sy:.5f})：宽高比被改变。"
            "通常是设备对输出又做了一次裁剪（例如 4:3 传感器出 16:9 视频）。"
            "请用 --crop L,T,W,H 传捕获时的 CaptureResult.SCALER_CROP_REGION；"
            "另外畸变系数 k1,k2,p1,p2,k3 只有在各向同性缩放下才能直接沿用。"
        )

    # 以下旋转按「连续坐标」推导（角点对映，忽略半像素差；对主点而言是亚像素级误差）
    #   u,v 为输出帧坐标，x,y 为 crop 内的传感器坐标
    if rotate == 0:
        return fx_s, fy_s, x, y
    if rotate == 180:  # 传感器左上角 -> 图像右下角
        return fx_s, fy_s, out_w - x, out_h - y
    if rotate == 90:  # 图像顺时针 90°（SENSOR_ORIENTATION=90 的后摄常见）：传感器 y 轴 -> 图像 u 轴
        return fy_s, fx_s, out_w - y, x
    if rotate == 270:  # 逆时针 90°
        return fy_s, fx_s, y, out_h - x
    raise SystemExit(f"--rotate 只支持 0/90/180/270，收到 {rotate}")


def compute(a) -> Intrinsics:
    warnings: list[str] = []
    fx, fy, cx, cy, skew, source = build_pixel_array_intrinsics(a)

    if a.center_crop:
        # 省掉手工算 crop：从 active array 居中裁出目标宽高比（16:9 输出配 4:3 传感器就是这么来的）。
        # 结果直接是【全像素阵列】坐标，所以下面不再叠加 active array 偏移。
        if a.active_array:
            al, at, aw, ah = parse_rect(a.active_array, "--active-array")
        elif a.pixel_array:
            pw, ph = parse_pair(a.pixel_array, "--pixel-array")
            al, at, aw, ah = 0, 0, int(pw), int(ph)
            warnings.append("--center-crop 没给 --active-array，退回用全像素阵列居中裁剪")
        else:
            raise SystemExit("--center-crop 需要 --active-array 或 --pixel-array")
        ar = parse_ratio(a.center_crop, "--center-crop")
        if aw / ah > ar:                      # 传感器比目标更宽 → 裁左右
            cw, ch = ah * ar, float(ah)
        else:                                 # 传感器比目标更高 → 裁上下（4:3 出 16:9 就是这种）
            cw, ch = float(aw), aw / ar
        left = al + (aw - cw) / 2.0
        top = at + (ah - ch) / 2.0
        crop = (round(left), round(top), round(cw), round(ch))
        print(f"[crop] --center-crop {a.center_crop} → L,T,W,H = {crop[0]},{crop[1]},{crop[2]},{crop[3]}"
              f"（在 {aw}x{ah} 的 active array 内居中）")
        if abs(cw / ch - ar) > 1e-3:
            warnings.append(f"居中裁剪后宽高比 {cw / ch:.4f} != 目标 {ar:.4f}（取整导致，影响可忽略）")
    elif a.crop:
        # SCALER_CROP_REGION 的坐标系是 active array，需要加上 active array 在 pixel array 里的偏移
        cl, ct, cw, ch = parse_rect(a.crop, "--crop")
        if a.active_array:
            al, at, _, _ = parse_rect(a.active_array, "--active-array")
            crop = (cl + al, ct + at, cw, ch)
        else:
            if a.active_offset:
                ox, oy = parse_pair(a.active_offset, "--active-offset")
                crop = (cl + int(ox), ct + int(oy), cw, ch)
            else:
                warnings.append("给了 --crop 但没给 --active-array/--active-offset，"
                                "假定 active array 在 pixel array 中偏移为 0")
                crop = (cl, ct, cw, ch)
    elif a.active_array:
        crop = parse_rect(a.active_array, "--active-array")
    else:
        raise SystemExit("需要 --active-array L,T,R,B（SENSOR_INFO_ACTIVE_ARRAY_SIZE）或 --crop")

    out_w, out_h = parse_pair(a.out, "--out")
    rotate = int(a.rotate)

    nfx, nfy, ncx, ncy = map_to_output(fx, fy, cx, cy, crop, out_w, out_h, rotate, warnings)

    dist = None
    if a.distortion:
        dist = nums(a.distortion)
        if len(dist) != 5:
            warnings.append(f"畸变系数应为 5 个 (k1,k2,p1,p2,k3)，收到 {len(dist)} 个")
    if abs(nfx - nfy) > 1e-3 and dist and any(abs(x) > 1e-9 for x in dist):
        warnings.append("fx != fy 却带了非零畸变系数：归一化坐标不同轴，畸变模型需要自行验证")

    return Intrinsics(nfx, nfy, ncx, ncy, out_w, out_h, source, skew, dist, warnings)


# --------------------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------------------

def fmt_yaml(i: Intrinsics) -> str:
    d = i.distortion or [0.0, 0.0, 0.0, 0.0, 0.0]
    return (
        "# 相机内参（单位：像素），参考帧 {w}x{h}\n"
        "Camera1.fx: {fx:.6f}\n"
        "Camera1.fy: {fy:.6f}\n"
        "Camera1.cx: {cx:.6f}\n"
        "Camera1.cy: {cy:.6f}\n"
        "\n"
        "# 畸变（OpenCV 径向+切向，来自 LENS_DISTORTION；无标定填 0）\n"
        "Camera1.k1: {k1:.6f}\n"
        "Camera1.k2: {k2:.6f}\n"
        "Camera1.p1: {p1:.6f}\n"
        "Camera1.p2: {p2:.6f}\n"
        "Camera1.k3: {k3:.6f}\n"
        "\n"
        "Camera.width: {w:d}\n"
        "Camera.height: {h:d}\n"
    ).format(
        w=int(i.width), h=int(i.height),
        fx=i.fx, fy=i.fy, cx=i.cx, cy=i.cy,
        k1=d[0], k2=d[1], p1=d[2], p2=d[3], k3=d[4],
    )


def fmt_json(i: Intrinsics) -> str:
    d = i.distortion or [0.0, 0.0, 0.0, 0.0, 0.0]
    return json.dumps(
        {
            "width": int(i.width),
            "height": int(i.height),
            "fx": round(i.fx, 6),
            "fy": round(i.fy, 6),
            "cx": round(i.cx, 6),
            "cy": round(i.cy, 6),
            "k1": d[0], "k2": d[1], "p1": d[2], "p2": d[3], "k3": d[4],
            "hfov_deg": round(i.hfov_deg, 3),
            "vfov_deg": round(i.vfov_deg, 3),
            "source": i.source,
        },
        ensure_ascii=False, indent=2,
    )


def patch_yaml(path: str, i: Intrinsics) -> None:
    """把算出的内参写回 ORB-SLAM3 风格 YAML，只改数字，保留注释。"""
    d = i.distortion or [0.0, 0.0, 0.0, 0.0, 0.0]
    repl = {
        "Camera1.fx": i.fx, "Camera1.fy": i.fy, "Camera1.cx": i.cx, "Camera1.cy": i.cy,
        "Camera1.k1": d[0], "Camera1.k2": d[1], "Camera1.p1": d[2], "Camera1.p2": d[3],
        "Camera1.k3": d[4],
        "Camera.width": int(i.width), "Camera.height": int(i.height),
    }
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    changed, out = [], []
    for line in lines:
        m = re.match(r"^(\s*)([A-Za-z0-9_.]+)(\s*:\s*)([-+0-9.eE]*)([^\n]*)(\n?)$", line)
        if m and m.group(2) in repl:
            key, old = m.group(2), m.group(4)
            new = f"{repl[key]:.6f}" if key not in ("Camera.width", "Camera.height") else str(repl[key])
            if old != new:
                changed.append(f"  {key}: {old} -> {new}")
                line = f"{m.group(1)}{key}{m.group(3)}{new}{m.group(5)}{m.group(6)}"
        out.append(line)
    if not changed:
        print(f"[patch] {path} 无需修改")
        return
    print(f"[patch] {path} 将修改：")
    print("\n".join(changed))
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    print(f"[patch] 已写入 {path}（原文件未备份，建议先 git diff 确认）")


# --------------------------------------------------------------------------------------

def list_cameras(path: str) -> None:
    """打印 dump 里每颗相机的关键静态量：用来挑 --hal、确认朝向、交叉验证标定值。"""
    blocks = split_static_blocks(path)
    if not blocks:
        print(f"[dump] {path} 没有 `== Camera HAL device ... static information: ==` 分块，"
              f"按「整份 dump = 单颗相机」处理")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            blocks = [(path, fh.readlines())]
    print(f"[dump] {path}：{len(blocks)} 个 HAL 静态块"
          f"（顺序通常 = camera id 顺序，后置主摄多为 0）\n")

    def row(vals, fmt="{:g}"):
        return " ".join(fmt.format(v) for v in vals) if vals else "—"

    for i, (title, lines) in enumerate(blocks):
        f = parse_dump_text(lines)
        raw = f.get("_raw", {})
        print(f"[HAL {i}] {title}")
        print(f"  Facing / Orientation : {raw.get('Facing', '—')} / {row(f.get('orientation'))}")
        fmm = (f.get("focal_mm") or [None])[0]
        phys = f.get("physical_size_mm")
        pix = f.get("pixel_array")
        act = f.get("active_array")
        print(f"  focal(mm)            : {fmm if fmm is not None else '—'}"
              f"      physicalSize(mm): "
              f"{'x'.join(f'{x:.4f}' for x in phys) if phys else '—'}")
        print(f"  pixelArray           : "
              f"{'x'.join(f'{int(x)}' for x in pix) if pix else '—'}"
              f"      activeArray(L,T,R,B): {row(act, '{:.0f}')}")
        k = f.get("intrinsic")
        if k:
            print(f"  intrinsicCalibration : fx={k[0]:.3f} fy={k[1]:.3f} "
                  f"cx={k[2]:.3f} cy={k[3]:.3f} s={k[4] if len(k) > 4 else 0:.4f}")
        else:
            print("  intrinsicCalibration : null（厂商没填 → 只能靠焦距推导 + 实拍标定）")
        d = f.get("distortion")
        print(f"  distortion(k1k2p1p2k3): {row(d, '{:.6f}')}"
              + ("   ← 全 0：设备未报畸变（不一定是真没畸变）" if d and not any(d) else ""))
        if fmm and phys and pix and phys[0] and pix[0]:
            dx = fmm / (phys[0] / pix[0])
            dy = fmm / (phys[1] / pix[1])
            line = f"  由焦距推导 fx/fy     : {dx:.1f} / {dy:.1f}   (像素间距 {phys[0] / pix[0] * 1000:.3f}µm)"
            if k:
                line += f"   与标定差 {(dx - k[0]) / k[0] * 100:+.1f}%"
            print(line)
        print()
    print("选好相机后：--dump <file> --hal <N> --crop-ltwh L,T,W,H --out W,H --rotate 0|90|180|270")


# --------------------------------------------------------------------------------------

def _normalize_argv(argv: list[str]) -> list[str]:
    """argparse 会把 '-0.12,0.015,...' 当成选项名。这里把 '--opt <-0.xx...>' 改写成 '--opt=<-0.xx...>'。"""
    value_opts = {
        "--dump", "--hal", "--intrinsic", "--focal-mm", "--sensor-mm", "--pixel-array",
        "--active-array", "--active-array-whxy", "--active-offset", "--crop", "--crop-ltwh",
        "--distortion", "--out", "--rotate", "--patch",
    }
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if (a in value_opts and i + 1 < len(argv)
                and argv[i + 1].startswith("-") and not argv[i + 1].startswith("--")):
            out.append(f"{a}={argv[i + 1]}")
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description="Android CameraCharacteristics -> 针孔内参 (fx,fy,cx,cy,k1,k2,p1,p2,k3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_argument_group("输入：二选一")
    src.add_argument("--dump", help="adb shell dumpsys media.camera 的输出文件")
    src.add_argument("--hal", type=int, default=None,
                     help="多摄机型：选第几个 HAL 静态块（0=通常后置主摄）。先跑 --list-cameras")
    src.add_argument("--list-cameras", action="store_true",
                     help="只列出 dump 里每颗相机的关键静态量（朝向/焦距/阵列/标定/畸变）后退出")
    src.add_argument("--intrinsic", help="LENS_INTRINSIC_CALIBRATION: fx,fy,cx,cy[,s]（像素）")
    src.add_argument("--focal-mm", dest="focal_mm", type=float,
                     help="LENS_INFO_AVAILABLE_FOCAL_LENGTHS[0]，单位 mm")
    src.add_argument("--sensor-mm", dest="sensor_mm", help="SENSOR_INFO_PHYSICAL_SIZE: W,H（mm）")
    src.add_argument("--pixel-array", dest="pixel_array", help="SENSOR_INFO_PIXEL_ARRAY_SIZE: W,H")
    src.add_argument("--active-array", dest="active_array", help="SENSOR_INFO_ACTIVE_ARRAY_SIZE: L,T,R,B")
    src.add_argument("--active-array-whxy", dest="active_array_whxy",
                     help="同上，但按 W,H,X,Y 解读")
    src.add_argument("--active-offset", dest="active_offset",
                     help="active array 在全像素阵列中的左上角偏移 X,Y（默认 0,0）")
    src.add_argument("--crop", help="捕获时 CaptureResult.SCALER_CROP_REGION: L,T,W,H（优先于 active array）")
    src.add_argument("--crop-ltwh", dest="crop_ltwh",
                     help="同上，但强制按 L,T,W,H 解读（--crop 是自动猜 L,T,R,B / W,H,X,Y，容易搞反）")
    src.add_argument("--center-crop", dest="center_crop",
                     help="不做手工 crop：从 active array 居中裁出该宽高比，如 16:9（16:9 输出配 4:3 传感器）")
    src.add_argument("--distortion", help="LENS_DISTORTION: k1,k2,p1,p2,k3")

    out = p.add_argument_group("输出")
    out.add_argument("--out", default="720,1280", help="目标帧尺寸 W,H（默认 720,1280）")
    out.add_argument("--rotate", default="0", choices=["0", "90", "180", "270"],
                     help="输出帧相对 active array 的顺时针旋转（竖屏录像通常是 90 或 270）")
    out.add_argument("--json", action="store_true", help="输出 JSON 而不是 YAML")
    out.add_argument("--patch", metavar="YAML", help="把结果写回 ORB-SLAM3 YAML 文件")
    out.add_argument("--list-keys", action="store_true", help="只列出 dump 里识别到的 key")

    a = p.parse_args(_normalize_argv(sys.argv[1:]))

    if a.dump and a.list_cameras:
        list_cameras(a.dump)
        return

    if a.dump:
        d = parse_dump(a.dump, a.hal)
        raw = d.pop("_raw", {})
        if a.list_keys:
            print(f"[dump] {a.dump} 中识别到的相关 key（HAL 块 {a.hal if a.hal is not None else 0}）：")
            for k, v in sorted(d.items()):
                print(f"  {k:22s} = {v}")
            if not d:
                print("  （无）—— 这台机器的 dumpsys 可能不含静态元数据。")
            print("[dump] dump 里其它 key（供对照，未识别）：")
            for k, v in list(raw.items())[:60]:
                print(f"  {k} = {v}")
            return
        a.intrinsic = a.intrinsic or _join(d.get("intrinsic"))
        a.distortion = a.distortion or _join(d.get("distortion"))
        a.focal_mm = a.focal_mm or (d.get("focal_mm") or [None])[0]
        a.sensor_mm = a.sensor_mm or _join(d.get("physical_size_mm"))
        a.pixel_array = a.pixel_array or _join(d.get("pixel_array"))
        a.active_array = a.active_array or _join(d.get("active_array"))
        if not a.intrinsic and not (a.focal_mm and a.sensor_mm):
            raise SystemExit(
                "[dump] 没有找到 intrinsicCalibration，也没有 focalLengths + physicalSize。"
                "用 --list-cameras / --list-keys 看看实际有哪些 key。"
            )
        for name in ("intrinsic", "distortion", "focal_mm", "sensor_mm", "pixel_array", "active_array"):
            if getattr(a, name) is not None:
                print(f"[dump] {name} = {getattr(a, name)}")

    if a.active_array_whxy:
        a.active_array = a.active_array_whxy
    if a.crop_ltwh:
        cl, ct, cw, ch = [int(round(x)) for x in nums(a.crop_ltwh)]
        a.crop = f"{cl},{ct},{cl + cw},{ct + ch}"     # 转成 Rect(L,T,R,B)，绕开自动猜测

    i = compute(a)

    print(f"\n== 结果（参考帧 {i.width:.0f}x{i.height:.0f}, rotate={a.rotate}）==")
    print(f"来源: {i.source}")
    print(f"fx={i.fx:.3f}  fy={i.fy:.3f}  cx={i.cx:.3f}  cy={i.cy:.3f}")
    print(f"水平 FOV ≈ {i.hfov_deg:.1f}°   垂直 FOV ≈ {i.vfov_deg:.1f}°")
    if i.distortion:
        print("畸变 k1,k2,p1,p2,k3 = " + ", ".join(f"{x:.6f}" for x in i.distortion))
    if i.skew:
        print(f"[warn] 出厂标定含 skew s={i.skew}，针孔模型不支持，忽略")
    for w in i.warnings:
        print(f"[warn] {w}")
    if not 0.3 < i.fx / i.width < 3.0:
        print(f"[warn] fx/W = {i.fx / i.width:.3f} 很反常，检查 active array / 旋转设置是否搞反了")

    print("\n== YAML（可直接贴进 ORB-SLAM3 配置）==")
    print(fmt_yaml(i))
    if a.json:
        print("== JSON ==")
        print(fmt_json(i))
    if a.patch:
        patch_yaml(a.patch, i)


def _join(v) -> str | None:
    if not v:
        return None
    return ",".join(f"{x:g}" for x in v)


if __name__ == "__main__":
    main()
