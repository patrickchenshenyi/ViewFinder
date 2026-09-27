#!/usr/bin/env python3
"""视频采集质量体检：分辨率 / 帧率 / 码率 / 每像素比特数(bpp)。

bpp = 视频码率 / (宽 × 高 × 帧率)，即"平均每个像素每秒分到多少比特"。
它才是决定压缩损失大小的量：在带宽固定时，提高分辨率或帧率都会压低 bpp。

工程经验（HEVC/H.265）：
  ≥ 0.25 bpp   接近无损观感，特征/纹理保留完整
  0.10–0.25    可接受，3DGS/SLAM 一般够用
  0.03–0.10    细节明显受损，ORB 特征变少、易出现块状/振铃伪影
  < 0.03       严重劣化，伪影会被重建当成真实几何

用法:
  python tools/probe_video.py 文件1.mp4 [文件2.mp4 ...]
"""
import json
import subprocess
import sys

BPP_BANDS = [
    (0.25, "接近无损"),
    (0.10, "可接受"),
    (0.03, "细节明显受损"),
    (0.00, "严重劣化"),
]


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if not v:
        return None
    num, den = (v.get("r_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    br = v.get("bit_rate")
    br = float(br) if br and br.isdigit() else None
    if br is None:  # 有些容器不写流码率，用总码率兜底
        br = float(info["format"].get("bit_rate", 0)) or None
    dur = float(info["format"].get("duration", 0) or 0)
    nbf = v.get("nb_frames")
    nbf = int(nbf) if nbf and nbf.isdigit() else (int(dur * fps) if fps else 0)
    bpp = (br / (w * h * fps)) if (br and w and h and fps) else None
    return {"path": path, "codec": v.get("codec_name"), "w": w, "h": h, "fps": fps,
            "br": br, "dur": dur, "frames": nbf, "bpp": bpp}


def band(bpp):
    for lo, name in BPP_BANDS:
        if bpp >= lo:
            return name
    return "?"


def main(paths):
    print(f"{'文件':<28}{'编码':>6}{'分辨率':>12}{'帧率':>7}{'码率':>10}{'时长':>7}{'帧数':>7}{'bpp':>8}  评价")
    rows = []
    for p in paths:
        r = probe(p)
        if not r:
            print(f"{p:<28}  无法解析视频流")
            continue
        rows.append(r)
        res = f"{r['w']}x{r['h']}"
        print(f"{p.split('/')[-1]:<28}{r['codec']:>6}{res:>12}"
              f"{r['fps']:>7.1f}{r['br']/1000:>9.0f}k{r['dur']:>7.1f}s{r['frames']:>7}"
              f"{r['bpp']:>8.3f}  {band(r['bpp'])}")

    if rows:
        print("\n参考：同样码率下换分辨率会怎样（HEVC 经验值）")
        print(f"{'场景':<34}{'码率':>9}{'分辨率':>12}{'帧率':>7}{'bpp':>8}  评价")
        for name, br, w, h, fps in [
            ("常见的'省流'直播设置", 2e6, 1280, 720, 30),
            ("同样的 2 Mbps 改推 1080p", 2e6, 1920, 1080, 30),
            ("同样的 2 Mbps 且降到 15fps", 2e6, 1280, 720, 15),
            ("手机上'高质量'720p 录制", 10e6, 1280, 720, 30),
            ("手机上'高质量'1080p 录制", 16e6, 1920, 1080, 30),
        ]:
            b = br / (w * h * fps)
            print(f"{name:<34}{br/1e6:>8.1f}M{f'{w}x{h}':>12}{fps:>7}{b:>8.3f}  {band(b)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1:])
