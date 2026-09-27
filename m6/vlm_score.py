#!/usr/bin/env python3
"""M6.4.1: VLM 打分 + 结构化评价 → top-3 效果图 + 位姿 + 解释文案。

两段式「全员入围 → 锦标赛」（修复：原 `top20[:topk]` 会把 F-B/C/D 族一刀切掉）：
  round1：screen 的**全部桶代表**（~41）按 family 分层分批（--batch），每批 VLM 选 top-K（--batch-top），并行；
  round2：幸存者一次 VLM 调用决出 top-3（--stage2-top）。
保证每个桶的最佳候选都被 VLM 真正看过、判过分。

用法（云端）：
  python vlm_score.py --l1 screen.json --renderdir <渲染目录> \
      --out vlm_top3.json --outdir /tmp/m6vlm \
      --batch 8 --batch-top 2 --stage2-top 3 --key <KEY>
"""
import argparse
import base64
import io
import json
import os
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash-vision-exp"


def call_vlm(images, text, key, max_tokens=16000, retries=3):
    content = []
    for name, b64 in images:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + b64}})
        content.append({"type": "text", "text": f"[候选 {name}]"})
    content.append({"type": "text", "text": text})
    body = {"model": MODEL, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens}
    last = ""
    last_reasoning = ""
    for attempt in range(retries):
        req = urllib.request.Request(API, data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=300))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:500]}")
        ch = r["choices"][0]
        msg = ch["message"]
        last = msg.get("content") or ""
        last_reasoning = msg.get("reasoning_content") or ""
        fr = ch.get("finish_reason")
        # 正常：content 有内容且未被截断
        if last.strip() and fr != "length":
            return last
        # content 空 或 被 max_tokens 截断：升预算重试（推理模型会先把 tokens 全花在 reasoning_content 上，
        # 8k 预算经常导致 content=0 + finish_reason=length；翻倍后通常能出 content）
        if fr == "length":
            body["max_tokens"] = min(int(body["max_tokens"] * 1.6), 32000)
            print(f"[vlm] finish=length → max_tokens 升到 {body['max_tokens']} 重试（attempt {attempt+1}/{retries}）")
        else:
            print(f"[vlm] content 空（finish={fr}）→ 重试（attempt {attempt+1}/{retries}）")
        time.sleep(1)
    # 兜底：content 始终为空时，reasoning_content 里往往已经写好了 JSON
    if not last.strip() and last_reasoning.strip():
        print("[vlm] content 空，回退用 reasoning_content")
        return last_reasoning
    return last


def img_b64(path, max_w=640):
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_w, max_w))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def extract_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return m.group(1)
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        return m.group(0)
    return text


def stratify_batches(items, batch_size):
    """按 family 分层轮询，把 items 切成每批 ≤batch_size 的「混合族」批次。

    例 items=[A0,A1,A2, B0,B1, C0]（已按族分堆），batch=3 →
    [A0,B0,C0], [A1,B1], [A2]。保证每批跨族、避免整批同族自相残杀。
    """
    by_fam = {}
    for it in items:
        by_fam.setdefault(it.get("family", "?"), []).append(it)
    fams = sorted(by_fam)
    batches, cur, i = [], [], 0
    while True:
        added = False
        for f in fams:
            if i < len(by_fam[f]):
                cur.append(by_fam[f][i])
                added = True
                if len(cur) == batch_size:
                    batches.append(cur)
                    cur = []
        if not added:
            break
        i += 1
    if cur:
        batches.append(cur)
    return batches


def load_candidate_images(names, cands, renderdir):
    """候选名 → [(name, b64)]；低覆盖率候选换 _crop_43 裁切图（裁掉残缺边）。"""
    images = []
    for name in names:
        p = os.path.join(renderdir, name + ".png")
        c = cands.get(name, {})
        if c.get("coverage", 1.0) < 0.7:
            pc = os.path.join(renderdir, name + "_crop_43.png")
            if os.path.exists(pc):
                p = pc
        if os.path.exists(p):
            images.append((name, img_b64(p)))
    return images


def parse_top(raw, key):
    """解析 VLM 返回 JSON，取 key 指定的列表；失败返回 None。"""
    try:
        data = json.loads(extract_json(raw))
        return data.get(key) or []
    except Exception as e:
        print(f"[vlm] JSON 解析失败（{e}），原文前 300 字符：\n{raw[:300]}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--l1", required=True)
    ap.add_argument("--renderdir", required=True)
    ap.add_argument("--out", default="/root/m6/vlm_top3.json")
    ap.add_argument("--outdir", default="/tmp/m6vlm")
    ap.add_argument("--batch", type=int, default=8)       # round1 每批候选数
    ap.add_argument("--batch-top", type=int, default=2)   # round1 每批选多少个
    ap.add_argument("--stage2-top", type=int, default=3)  # round2 最终 top 数
    ap.add_argument("--key", required=True)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    from vlm_prompts import SELECT_PROMPT_TMPL, SELECT_BATCH_PROMPT

    l1 = json.load(open(args.l1))
    # 用「全部桶代表」（selected），而不是 top20[:topk] —— 让每个桶的最佳候选都参与竞选
    items = l1.get("selected") or l1.get("top20") or []
    cands = {c["name"]: c for c in items}
    fams = {}
    for c in items:
        fams[c.get("family", "?")] = fams.get(c.get("family", "?"), 0) + 1
    print(f"[vlm] screen 桶代表 {len(items)} 个，family 分布 {fams}")

    batches = stratify_batches(items, args.batch)
    print(f"[vlm] round1：{len(batches)} 批（每批 ≤{args.batch}、混合族），每批选 {args.batch_top}，并行")

    def run_batch(b):
        names = [c["name"] for c in b]
        imgs = load_candidate_images(names, cands, args.renderdir)
        if not imgs:
            return []
        prompt = SELECT_BATCH_PROMPT.format(
            n=len(imgs), k=min(args.batch_top, len(imgs)),
            names=json.dumps([n for n, _ in imgs], ensure_ascii=False))
        raw = call_vlm(imgs, prompt, args.key)
        top = parse_top(raw, "top")
        if top is None:
            # 解析失败：保底取本批 coverage 最高的 batch_top 个
            order = sorted(b, key=lambda c: -(c.get("coverage", 0.0)))[:args.batch_top]
            print(f"[vlm] round1 某批解析失败，保底 coverage 前 {len(order)}："
                  f"{[c['name'] for c in order]}")
            return [{"name": c["name"], "reason": ""} for c in order]
        got = [t for t in top if t.get("name") in set(names)]
        print(f"[vlm]   round1 批返回 {len(got)}：{[t['name'] for t in got]}")
        return got

    survivors = []
    if len(batches) <= 1:
        survivors = run_batch(batches[0]) if batches else []
    else:
        with ThreadPoolExecutor(max_workers=min(len(batches), 6)) as ex:
            for got in ex.map(run_batch, batches):
                survivors += got

    # 跨批去重（保持顺序）
    seen = set()
    uniq = []
    for s in survivors:
        if s["name"] not in seen:
            seen.add(s["name"])
            uniq.append(s)
    survivors = uniq
    print(f"[vlm] round1 幸存者 {len(survivors)} 个：{[s['name'] for s in survivors]}")

    if not survivors:
        print("[vlm] 无幸存者，中止")
        raise SystemExit(1)

    # round2：幸存者 → top-3
    imgs2 = load_candidate_images([s["name"] for s in survivors], cands, args.renderdir)
    prompt2 = SELECT_PROMPT_TMPL.format(
        n=len(imgs2), names=json.dumps([n for n, _ in imgs2], ensure_ascii=False))
    raw2 = call_vlm(imgs2, prompt2, args.key)
    print(f"[vlm] round2 原始返回 {len(raw2)} 字符：\n{raw2[:400]}")
    top3 = parse_top(raw2, "top3")
    if top3 is None:
        top3 = [{"name": s["name"], "reason": s.get("reason", "")}
                for s in survivors[:args.stage2_top]]
        print(f"[vlm] round2 解析失败，保底取幸存者前 {len(top3)}")

    result = []
    for item in top3[:args.stage2_top]:
        name = item.get("name")
        if name in cands:
            c = cands[name]
            result.append({"name": name, "reason": item.get("reason", ""),
                           "R": c["R"], "C": c["C"], "uv": c["uv"],
                           "proto": c.get("proto"), "subject": c.get("subject"),
                           "family": c.get("family")})
        else:
            print(f"[vlm] 警告：VLM 返回候选名 '{name}' 不在候选集，跳过")

    print(f"[vlm] top-{len(result)}：")
    for i, r in enumerate(result, 1):
        print(f"  {i}. {r['name']}（{r.get('family')}/subj{r.get('subject')}）— {r['reason']}")

    json.dump({"model": MODEL, "top3": result}, open(args.out, "w"),
              indent=1, ensure_ascii=False)
    print(f"[vlm] 已写 {args.out}")

    # top-3 contact sheet
    cells = []
    for r in result:
        p = os.path.join(args.renderdir, r["name"] + ".png")
        if os.path.exists(p):
            cells.append(np.asarray(Image.open(p).convert("RGB")))
    if cells:
        cw, ch = 480, 270
        sheet = np.zeros((ch, cw * len(cells), 3), np.uint8)
        for i, im in enumerate(cells):
            sheet[:, i * cw:(i + 1) * cw] = np.asarray(
                Image.fromarray(im).resize((cw, ch), Image.LANCZOS))
        Image.fromarray(sheet).save(os.path.join(args.outdir, "vlm_top3.png"))
        print(f"[vlm] top-3 sheet: {os.path.join(args.outdir, 'vlm_top3.png')}")


if __name__ == "__main__":
    main()
