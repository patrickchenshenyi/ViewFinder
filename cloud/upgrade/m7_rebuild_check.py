#!/usr/bin/env python3
# M7.1 验收：从 full_<iter>.bin（id-indexed 全量）重建 PLY，并与 snapshot.ply 逐字节对比。
# 若逐字节一致，证明 saveSplatFull 的 62-float 序列化与 savePly 完全同构。
# 用法：python3 m7_rebuild_check.py [deltas_dir] [snapshot.ply]
#   默认：deltas_dir = ~/Photo-SLAM/results/m5_live/live/deltas
#         snapshot    = ~/Photo-SLAM/results/m5_live/live/snapshot.ply
import sys, os, struct, glob

N_FLOATS = 62
FIELDS = (
    ['x', 'y', 'z', 'nx', 'ny', 'nz', 'f_dc_0', 'f_dc_1', 'f_dc_2']
    + ['f_rest_%d' % i for i in range(45)]
    + ['opacity', 'scale_0', 'scale_1', 'scale_2', 'rot_0', 'rot_1', 'rot_2', 'rot_3']
)
assert len(FIELDS) == N_FLOATS

def header_text(count):
    props = '\n'.join('property float %s' % f for f in FIELDS)
    return 'ply\nformat binary_little_endian 1.0\nelement vertex %d\n%s\nend_header\n' % (count, props)

def latest_full(deltas_dir):
    files = glob.glob(os.path.join(deltas_dir, 'full_*.bin'))
    if not files:
        return None
    files.sort(key=lambda f: int(os.path.basename(f).split('_')[1].split('.')[0]))
    return files[-1]

def read_full(path):
    with open(path, 'rb') as f:
        data = f.read()
    count = struct.unpack_from('<I', data, 0)[0]
    off = 4
    verts = []
    for _ in range(count):
        off += 8  # skip i64 id
        vals = struct.unpack_from('<%df' % N_FLOATS, data, off)
        off += N_FLOATS * 4
        verts.append(vals)
    return verts

def rebuild_ply(verts):
    hdr = header_text(len(verts)).encode('ascii')
    body = b''.join(struct.pack('<%df' % N_FLOATS, *v) for v in verts)
    return hdr + body

def main():
    deltas_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        '~/Photo-SLAM/results/m5_live/live/deltas')
    snap = sys.argv[2] if len(sys.argv) > 2 else os.path.expanduser(
        '~/Photo-SLAM/results/m5_live/live/snapshot.ply')

    fp = latest_full(deltas_dir)
    if not fp:
        print('FAIL: no full_*.bin in', deltas_dir)
        sys.exit(1)
    print('full file:', fp)

    verts = read_full(fp)
    print('gaussians in full:', len(verts))

    if not os.path.exists(snap):
        print('FAIL: snapshot.ply not found:', snap)
        sys.exit(1)
    snap_data = open(snap, 'rb').read()
    # snapshot.ply 顶点数（从头部解析）
    head = snap_data.split(b'end_header\n')[0].decode('ascii', 'ignore')
    n_snap = int([l for l in head.splitlines() if l.startswith('element vertex ')][0].split()[2])
    print('gaussians in snapshot.ply:', n_snap)
    if n_snap != len(verts):
        print('FAIL: count mismatch (full=%d snap=%d)' % (len(verts), n_snap))
        sys.exit(1)

    rebuilt = rebuild_ply(verts)
    print('rebuilt bytes:', len(rebuilt), 'snapshot bytes:', len(snap_data))
    if rebuilt == snap_data:
        print('PASS: full_*.bin 重建 PLY 与 snapshot.ply 逐字节一致 ✓')
        sys.exit(0)
    # 定位第一个不一致字节，帮助排查字段顺序
    for i, (a, b) in enumerate(zip(rebuilt, snap_data)):
        if a != b:
            print('FAIL: 首个不一致字节 @%d (rebuilt=%02x snap=%02x)' % (i, a, b))
            break
    sys.exit(1)

if __name__ == '__main__':
    main()
