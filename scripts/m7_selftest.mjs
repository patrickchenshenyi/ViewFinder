// M7 自检：验证「累计表 → PLY 重建」与「full/delta 二进制协议」的格式正确性。
// 关键断言：生成的头与 Photo-SLAM savePly 的头逐字节一致（除 element vertex 计数行）。
// 用法：node scripts/m7_selftest.mjs
//
// 注意：下面 PLY_FIELDS / plyHeaderText / buildPlyFromAccum / parseFullSync / parseDelta
// 是 tools/splat-web/dist/app.js 中 M7 块的镜像，改 app.js 时需同步这里。
import fs from 'fs';

const PLY_N_FLOATS = 62;
const PLY_FIELDS = [
  'x', 'y', 'z', 'nx', 'ny', 'nz',
  'f_dc_0', 'f_dc_1', 'f_dc_2',
  ...Array.from({ length: 45 }, (_, i) => `f_rest_${i}`),
  'opacity', 'scale_0', 'scale_1', 'scale_2', 'rot_0', 'rot_1', 'rot_2', 'rot_3',
];
function plyHeaderText(count) {
  const props = PLY_FIELDS.map((f) => `property float ${f}`).join('\n');
  return `ply\nformat binary_little_endian 1.0\nelement vertex ${count}\n${props}\nend_header\n`;
}

function buildPlyFromMap(map) {
  const ids = Array.from(map.keys()).sort((a, b) => a - b);
  const count = ids.length;
  const headerBytes = new TextEncoder().encode(plyHeaderText(count));
  const buf = new ArrayBuffer(headerBytes.byteLength + count * PLY_N_FLOATS * 4);
  new Uint8Array(buf, 0, headerBytes.byteLength).set(headerBytes);
  const dv = new DataView(buf, headerBytes.byteLength);
  let off = 0;
  for (const id of ids) {
    const p = map.get(id);
    for (let k = 0; k < PLY_N_FLOATS; k++) dv.setFloat32(off + k * 4, p[k], true);
    off += PLY_N_FLOATS * 4;
  }
  return buf;
}

function encodeFullSync(map) {
  const ids = Array.from(map.keys()).sort((a, b) => a - b);
  const bytes = 4 + ids.length * (8 + PLY_N_FLOATS * 4);
  const buf = new ArrayBuffer(bytes);
  const dv = new DataView(buf);
  dv.setUint32(0, ids.length, true);
  let off = 4;
  for (const id of ids) {
    dv.setBigInt64(off, BigInt(id), true); off += 8;
    const p = map.get(id);
    for (let k = 0; k < PLY_N_FLOATS; k++) dv.setFloat32(off + k * 4, p[k], true);
    off += PLY_N_FLOATS * 4;
  }
  return buf;
}

function parseFullSync(buf) {
  const dv = new DataView(buf);
  const count = dv.getUint32(0, true);
  let off = 4;
  const entries = [];
  for (let i = 0; i < count; i++) {
    const id = Number(dv.getBigInt64(off, true)); off += 8;
    const p = new Float32Array(PLY_N_FLOATS);
    for (let k = 0; k < PLY_N_FLOATS; k++) { p[k] = dv.getFloat32(off, true); off += 4; }
    entries.push({ id, params: p });
  }
  return entries;
}

function parseDelta(buf) {
  const dv = new DataView(buf);
  let off = 0;
  const addedN = dv.getUint32(off, true); off += 4;
  const removedN = dv.getUint32(off, true); off += 4;
  const added = [];
  for (let i = 0; i < addedN; i++) {
    const id = Number(dv.getBigInt64(off, true)); off += 8;
    const p = new Float32Array(PLY_N_FLOATS);
    for (let k = 0; k < PLY_N_FLOATS; k++) { p[k] = dv.getFloat32(off, true); off += 4; }
    added.push({ id, params: p });
  }
  const removed = [];
  for (let i = 0; i < removedN; i++) { removed.push(Number(dv.getBigInt64(off, true))); off += 8; }
  return { added, removed };
}

function encodeDelta({ added, removed }) {
  const bytes = 8 + added.length * (8 + PLY_N_FLOATS * 4) + removed.length * 8;
  const buf = new ArrayBuffer(bytes);
  const dv = new DataView(buf);
  let off = 0;
  dv.setUint32(off, added.length, true); off += 4;
  dv.setUint32(off, removed.length, true); off += 4;
  for (const e of added) {
    dv.setBigInt64(off, BigInt(e.id), true); off += 8;
    for (let k = 0; k < PLY_N_FLOATS; k++) dv.setFloat32(off + k * 4, e.params[k], true);
    off += PLY_N_FLOATS * 4;
  }
  for (const id of removed) { dv.setBigInt64(off, BigInt(id), true); off += 8; }
  return buf;
}

function gauss(x, y, z) {
  const p = new Float32Array(PLY_N_FLOATS);
  p[0] = x; p[1] = y; p[2] = z;                 // xyz
  p[6] = 0.5; p[7] = 0.6; p[8] = 0.7;          // f_dc
  p[54] = 0.9;                                  // opacity
  p[55] = 1.0; p[56] = 2.0; p[57] = 3.0;        // scale
  p[58] = 0.0; p[59] = 0.0; p[60] = 0.0; p[61] = 1.0; // rot
  return p;
}

let failures = 0;
function ok(cond, msg) {
  if (cond) console.log('  ✓ ' + msg);
  else { failures += 1; console.error('  ✗ ' + msg); }
}

// ---------- 1) 头与真实 Photo-SLAM PLY 逐字节一致（除 count 行） ----------
console.log('[1] header 与 Photo-SLAM savePly 一致');
const real = fs.readFileSync('tools/splat-web/dist/splats/m5_original.ply');
const realHeader = real.subarray(0, real.indexOf(Buffer.from('end_header\n')) + 'end_header\n'.length).toString();
const realCount = Number(realHeader.match(/element vertex (\d+)/)[1]);
const mine = plyHeaderText(realCount);
ok(mine === realHeader, 'plyHeaderText(102749) 与真实头逐字节一致');
if (mine !== realHeader) {
  const a = mine.split('\n'); const b = realHeader.split('\n');
  for (let i = 0; i < Math.max(a.length, b.length); i++)
    if (a[i] !== b[i]) console.error(`    line ${i}: mine=${JSON.stringify(a[i])} real=${JSON.stringify(b[i])}`);
}
ok(PLY_FIELDS.length === PLY_N_FLOATS, `PLY_FIELDS 长度 ${PLY_FIELDS.length} == 62`);

// ---------- 2) full 同步 round-trip ----------
console.log('[2] full 同步 round-trip');
const map0 = new Map([[0, gauss(1, 2, 3)], [5, gauss(-1, -2, -3)], [2, gauss(0.5, 0.5, 0.5)]]);
const full = encodeFullSync(map0);
const parsed = parseFullSync(full);
ok(parsed.length === 3, `full 解析出 3 条（实际 ${parsed.length}）`);
ok(parsed.every((e) => map0.has(e.id)), 'full 的 id 与输入一致');
const e2 = parsed.find((e) => e.id === 2);
const close = (a, b) => Math.abs(a - b) < 1e-5;
ok(e2 && close(e2.params[0], 0.5) && close(e2.params[1], 0.5) && close(e2.params[2], 0.5), 'id=2 的 xyz 值正确');
ok(e2 && close(e2.params[54], 0.9), 'id=2 的 opacity 在偏移 54');
ok(e2 && e2.params[55] === 1 && e2.params[58] === 0 && e2.params[61] === 1, 'scale/rot 偏移正确');

// ---------- 3) delta round-trip ----------
console.log('[3] delta round-trip');
const delta = encodeDelta({ added: [{ id: 9, params: gauss(9, 9, 9) }], removed: [5] });
const d = parseDelta(delta);
ok(d.added.length === 1 && d.removed.length === 1, 'delta 解析 added=1 removed=1');
ok(d.added[0].id === 9 && d.added[0].params[0] === 9, 'delta added id=9 的 x=9');
ok(d.removed[0] === 5, 'delta removed id=5');

// ---------- 4) 累计表增删后重建 PLY，解析核对 ----------
console.log('[4] 累计表重建 PLY');
const acc = new Map(map0);
for (const e of d.added) acc.set(e.id, e.params);
for (const id of d.removed) acc.delete(id);
ok(acc.size === 3, `增删后累计表 3 条（实际 ${acc.size}）`);
ok(acc.has(9) && !acc.has(5) && acc.has(0) && acc.has(2), 'id=9 加入、id=5 删除、其余保留');
const ply = buildPlyFromMap(acc);
const hdr = new TextDecoder().decode(ply).split('end_header')[0];
ok(/element vertex 3/.test(hdr), '重建 PLY 头 element vertex 3');
// 解析重建 PLY 的第一个顶点 xyz（按 id 升序：0 在前）
const dv = new DataView(ply);
const headerBytes = plyHeaderText(3).length;
const x0 = dv.getFloat32(headerBytes, true);
ok(x0 === 1, `重建 PLY 首顶点 x=1（id=0，实际 ${x0}）`);

// ---------- 汇总 ----------
console.log(failures === 0 ? '\nALL M7 SELFTEST PASS ✓' : `\n${failures} FAILURE(S) ✗`);
process.exit(failures === 0 ? 0 : 1);
