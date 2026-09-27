#!/usr/bin/env python3
"""M6.4.2: 反馈微调环（VLM 出意图 → 几何出数字 → 重渲染 → L1 门控，≤3 轮）。

对 top-3 候选逐个微调：
  每轮：渲染当前 → VLM 给结构化意图 {issue, fix, magnitude} → 几何求解器算位姿增量
        → 重渲染 → L1 规则分门控（改进才接受）→ 停滞则停。
原则：VLM 不给米/度，只给意图 + 幅度；数字由几何求解器算（杜绝 VLM 空间量化幻觉）。

fix 动作集（几何可解）：move_subject_left/right/up/down、enlarge/shrink_subject、none。
  move_left/right  → 绕 up 轴 yaw（相机右转→主体左移）
  move_up/down     → 相机下/上平移
  enlarge/shrink   → 沿视线前/后平移

用法（云端，需 LD_PRELOAD）：
  LD_PRELOAD=... python refine.py --vlm /root/m6/vlm_top3.json \
      --ply /root/m6/m6_fog_pruned.ply --cameras <cameras.json> \
      --out /root/m6/refine.json --outdir /tmp/m6refine --rounds 3 --key <KEY>
"""
import argparse
import base64
import io
import json
import math
import os
import re
import urllib.request

import numpy as np
from PIL import Image

import render_pose
from l1_score import cluster_subjects, rule_scores, project
from coverage import render_magenta, coverage_metrics
from freeview_audit import img_stats

API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash-vision-exp"
W = {"third": 0.40, "size": 0.20, "tilt": 0.10, "headroom": 0.10, "contrast": 0.20}
MAG = {"slight": 0.5, "moderate": 1.0, "strong": 1.6}


def rot_around(v, axis, th):
    axis = np.asarray(axis, np.float64); axis = axis / (np.linalg.norm(axis) + 1e-9)
    v = np.asarray(v, np.float64)
    return v * math.cos(th) + np.cross(axis, v) * math.sin(th) + axis * (axis @ v) * (1 - math.cos(th))


def compose_R(right, fwd):
    right = right / (np.linalg.norm(right) + 1e-9)
    fwd = fwd / (np.linalg.norm(fwd) + 1e-9)
    camY = np.cross(fwd, right)
    return np.stack([right, camY, fwd], axis=1)


def apply_fix(R, C, X, up, fix, magnitude):
    """意图 → 位姿增量（返回 R', C'）。"""
    m = MAG.get(magnitude, 1.0)
    fwd = R[:, 2]; right = R[:, 0]
    Xc = R.T @ (X - C)
    depth = max(Xc[2], 1e-3)
    if fix == "move_subject_left":   # 主体左移 → 相机左转（-th，实测 Δuu<0）
        th = math.radians(6 * m)
        nf = rot_around(fwd, up, -th); nr = rot_around(right, up, -th)
        return compose_R(nr, nf), C
    if fix == "move_subject_right":  # 主体右移 → 相机右转（+th，实测 Δuu>0）
        th = math.radians(6 * m)
        nf = rot_around(fwd, up, th); nr = rot_around(right, up, th)
        return compose_R(nr, nf), C
    if fix == "move_subject_up":     # 相机下移 → 主体上移
        return R, C - up * (0.10 * depth * m)
    if fix == "move_subject_down":
        return R, C + up * (0.10 * depth * m)
    if fix == "enlarge_subject":     # 前移 → 更大
        return R, C + fwd * (0.25 * depth * m)
    if fix == "shrink_subject":      # 后移 → 更小
        return R, C - fwd * (0.25 * depth * m)
    return R, C  # none / unknown


def arr_b64(img):
    im = Image.fromarray(img)
    im.thumbnail((640, 640))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def call_vlm_intent(img, key):
    from vlm_prompts import INTENT_PROMPT
    body = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + arr_b64(img)}},
        {"type": "text", "text": INTENT_PROMPT}]}],
        "max_tokens": 8000}
    req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=300))
    return r["choices"][0]["message"]["content"]


def _vlm_chat(content, key, max_tokens=4000, retries=2):
    """带空返回/截断重试的通用 chat 调用。"""
    body = {"model": MODEL, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens}
    last = ""
    for _ in range(retries):
        req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=300))
        last = r["choices"][0]["message"]["content"]
        if last and last.strip() and r["choices"][0].get("finish_reason") != "length":
            return last
    return last


def pairwise_better(img_old, img_new, key):
    """M6.6.3 VLM 成对门控：顺序互换问两次、一致才采信。

    返回 'new'（新视角更好）/ 'old'（旧视角更好）/ 'tie'（不一致，保守回退）。
    """
    from vlm_prompts import PAIRWISE_PROMPT

    def ask(first, second):
        content = [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + arr_b64(first)}},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + arr_b64(second)}},
            {"type": "text", "text": PAIRWISE_PROMPT}]
        return _vlm_chat(content, key)

    def decode(raw):
        if not raw:
            return None
        m = re.search(r'"better"\s*:\s*"([12])"', raw)
        if m:
            return m.group(1)
        m = re.search(r"\b([12])\b", raw)
        return m.group(1) if m else None

    a1 = decode(ask(img_old, img_new))   # 问1：1=old, 2=new
    a2 = decode(ask(img_new, img_old))   # 问2：1=new, 2=old
    if a1 is None or a2 is None:
        return "tie"
    p1 = "old" if a1 == "1" else "new"
    p2 = "new" if a2 == "1" else "old"
    if p1 == p2 == "new":
        return "new"
    if p1 == p2 == "old":
        return "old"
    return "tie"


def degenerate_guard(subj, R, C, fx, fy, w, h):
    """M6.6.3 确定性退化守卫：主体在相机后 / 主体出画。返回原因或 None。"""
    pr = project(R, C, subj["center"], fx, fy, w, h)
    if pr is None:
        return "subject_behind_camera"
    u, v, _ = pr
    if u < -0.3 * w or u > 1.3 * w or v < -0.3 * h or v > 1.3 * h:
        return "subject_off_frame"
    return None


def extract_json(text):
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start < 0:
        return text
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


def l1_of(img, R, C, subj, up, fx, fy, w, h, intent=None):
    st = img_stats(img)
    comp = rule_scores(R, C, subj, up, fx, fy, w, h, st["tenengrad"], intent)
    return sum(W[k] * comp[k] for k in W), comp, st["tenengrad"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vlm", required=True)
    ap.add_argument("--ply", required=True)
    ap.add_argument("--cameras", required=True)
    ap.add_argument("--out", default="/root/m6/refine.json")
    ap.add_argument("--outdir", default="/tmp/m6refine")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--key", required=True)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    model = render_pose.load_model(args.ply)
    cams = json.load(open(args.cameras))
    fx, fy = cams[0]["fx"], cams[0]["fy"]
    w, h = cams[0]["width"], cams[0]["height"]
    up = np.asarray(json.load(open("/root/m6/l1_top_v2.json"))["up"], np.float64)

    from plyfile import PlyData
    v = PlyData.read(args.ply)["vertex"]
    xyz = np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    subjects = cluster_subjects(xyz)

    vlm = json.load(open(args.vlm))
    top3 = vlm["top3"]

    def render(R, C):
        return render_pose.render_pose(model, render_pose.build_camera(R, C, fx, fy, w, h))

    # ---------- 几何方向自检 ----------
    if args.selftest:
        c = top3[0]
        R = np.asarray(c["R"], np.float64); C = np.asarray(c["C"], np.float64)
        subj = subjects[c["subject"]]
        base = project(R, C, subj["center"], fx, fy, w, h)
        print(f"[selftest] 基线 subject{c['subject']} uv=({base[0]/w:.3f},{base[1]/h:.3f}) depth={base[2]:.2f}")
        for fix in ("move_subject_left", "move_subject_right", "move_subject_up",
                    "move_subject_down", "enlarge_subject", "shrink_subject"):
            R2, C2 = apply_fix(R, C, subj["center"], up, fix, "moderate")
            p2 = project(R2, C2, subj["center"], fx, fy, w, h)
            if p2 is None:
                print(f"  {fix}: 主体跑到相机后（无效）")
                continue
            d_uu = p2[0] / w - base[0] / w
            d_vv = p2[1] / h - base[1] / h
            d_size = base[2] / p2[2]  # 深度比，>1 = 更近更大
            print(f"  {fix:<20} Δuu={d_uu:+.3f} Δvv={d_vv:+.3f} 深度比={d_size:.2f}")
        return

    # ---------- 反馈微调环 ----------
    out = []
    for ci, c in enumerate(top3):
        name = c["name"]
        R = np.asarray(c["R"], np.float64); C = np.asarray(c["C"], np.float64)
        subj = subjects[c["subject"]]
        intent = c.get("proto")
        img = render(R, C)
        log = [{"round": 0, "fix": None, "accepted": True}]
        print(f"\n=== 候选 {name}（VLM 成对门控，≤{args.rounds} 轮）===")
        for rnd in range(1, args.rounds + 1):
            raw = call_vlm_intent(img, args.key)
            if not raw or not raw.strip():
                print(f"  [round {rnd}] VLM 返回空（token 不够？），重试一次…")
                raw = call_vlm_intent(img, args.key)
            if not raw or not raw.strip():
                print(f"  [round {rnd}] 重试仍空，跳过本轮")
                break
            try:
                d = json.loads(extract_json(raw))
            except Exception as e:
                print(f"  [round {rnd}] VLM JSON 解析失败（{e}），原文: {raw[:200] if raw else '(空)'}")
                break
            fix, mag = d.get("fix", "none"), d.get("magnitude", "moderate")
            if fix == "none":
                print(f"  [round {rnd}] VLM 说已足够好（none），停止")
                break
            R2, C2 = apply_fix(R, C, subj["center"], up, fix, mag)
            # 确定性退化守卫（M6.6.3）
            guard = degenerate_guard(subj, R2, C2, fx, fy, w, h)
            if guard:
                print(f"  [round {rnd}] 守卫否决（{guard}），回退并停止  issue={d.get('issue','')[:40]}")
                log.append({"round": rnd, "fix": fix, "magnitude": mag,
                            "issue": d.get("issue", ""), "accepted": False, "guard": guard})
                break
            img2 = render(R2, C2)
            cm2 = coverage_metrics(render_magenta(model, R2, C2, fx, fy, w, h))
            if cm2["coverage"] < 0.05:
                print(f"  [round {rnd}] 守卫否决（新视角 coverage {cm2['coverage']:.2f} 不可判读），回退并停止")
                log.append({"round": rnd, "fix": fix, "magnitude": mag,
                            "issue": d.get("issue", ""), "accepted": False, "guard": "unjudgeable"})
                break
            verdict = pairwise_better(img, img2, args.key)
            accepted = verdict == "new"
            print(f"  [round {rnd}] fix={fix}/{mag} → VLM 成对判定={verdict} "
                  f"({'接受✅' if accepted else '回退❌'})  issue={d.get('issue','')[:40]}")
            log.append({"round": rnd, "fix": fix, "magnitude": mag,
                        "issue": d.get("issue", ""), "verdict": verdict, "accepted": accepted})
            if accepted:
                R, C, img = R2, C2, img2
            else:
                break  # 平局/回退 → 停滞停止
        # 保存最终渲染（before + after 拼图）
        img0 = render(np.asarray(c["R"], np.float64), np.asarray(c["C"], np.float64))
        Image.fromarray(np.concatenate([img0, img], axis=1)).save(
            os.path.join(args.outdir, f"refine_{name}.png"))
        n_accepted = sum(1 for r in log[1:] if r.get("accepted"))
        out.append({"name": name, "family": c.get("family"), "subject": c["subject"],
                    "rounds": log, "n_accepted": n_accepted,
                    "R": R.tolist(), "C": C.tolist()})
        print(f"  共 {len(log)-1} 轮，接受 {n_accepted} 轮（VLM 成对门控）")

    json.dump({"model": MODEL, "refined": out}, open(args.out, "w"), indent=1, ensure_ascii=False)
    print(f"\n[refine] 已写 {args.out}")


if __name__ == "__main__":
    main()
