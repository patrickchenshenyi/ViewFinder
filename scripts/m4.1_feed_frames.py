#!/usr/bin/env python3
# M4.1 测试辅助：把 ~/live_trim 的前 N 帧「喂」进 ~/live_inc，并重建 rgb.txt
# 用法：python3 m4.1_feed_frames.py <N>
import os, sys, shutil

src = os.path.expanduser('~/live_trim/rgb')
dst = os.path.expanduser('~/live_inc/rgb')
N = int(sys.argv[1])
os.makedirs(dst, exist_ok=True)

for i in range(1, N + 1):
    f = '%06d.png' % i
    if not os.path.exists(os.path.join(dst, f)):
        shutil.copy(os.path.join(src, f), os.path.join(dst, f))

files = sorted(f for f in os.listdir(dst) if f.endswith('.png'))
with open(os.path.expanduser('~/live_inc/rgb.txt'), 'w') as w:
    w.write('# color images\n# file\n# timestamp filename\n')
    for f in files:
        num = int(f.split('.')[0])
        w.write('%.6f rgb/%s\n' % ((num - 1) / 10.0, f))
print('fed %d frames' % len(files))
