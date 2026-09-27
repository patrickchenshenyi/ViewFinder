#!/usr/bin/env python3
"""M6.6.1 验收测试：新提示词效果。

(a) 重评 top-12：新 top-3 与旧 top-3 对比。
(b) 混入用户实拍（my_shot）：VLM 是否把它排前列、点评是否基于视点而非画质。

用法（云端）：
  python m66_test.py --key <KEY> --myshot /tmp/m6_my_shot.jpg --out /root/m6/m66_test.json
"""
import argparse
import json
import os

from vlm_score import call_vlm, img_b64
from vlm_prompts import SELECT_PROMPT_TMPL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--myshot", required=True)
    ap.add_argument("--out", default="/root/m6/m66_test.json")
    args = ap.parse_args()

    l1 = json.load(open("/root/m6/l1_top_v2.json"))
    order = [c["name"] for c in l1["top20"][:12]]
    images = []
    for n in order:
        p = f"/tmp/m6cand3/{n}.png"
        if os.path.exists(p):
            images.append((n, img_b64(p)))
    print(f"[m66] 候选 {len(images)} 张")

    # (a) 重评 top-12
    prompt = SELECT_PROMPT_TMPL.format(
        n=len(images), names=json.dumps([n for n, _ in images], ensure_ascii=False))
    raw_a = call_vlm(images, prompt, args.key)
    print("===== (a) 重评 top-12（新提示词）=====")
    print(raw_a[:800])

    # (b) 混入用户实拍
    images_b = images + [("my_shot", img_b64(args.myshot))]
    prompt_b = SELECT_PROMPT_TMPL.format(
        n=len(images_b), names=json.dumps([n for n, _ in images_b], ensure_ascii=False))
    raw_b = call_vlm(images_b, prompt_b, args.key)
    print("\n===== (b) 混入用户实拍 my_shot（新提示词）=====")
    print(raw_b[:800])

    json.dump({"a": raw_a, "b": raw_b}, open(args.out, "w"), ensure_ascii=False, indent=1)
    print(f"\n[m66] 已写 {args.out}")


if __name__ == "__main__":
    main()
