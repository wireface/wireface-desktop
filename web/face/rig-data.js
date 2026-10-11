// The data boundary shared by Creator and playback. Version 1 uses the 468 vertex Wireface topology.
import canonical from './canonical-data.js';
import { CH } from './rig.js';

const fail = name => { throw new TypeError(`Invalid character: ${name}`); };
export function numbers(value, length, name, { indices = false, max = 468 } = {}) {
  if (!(Array.isArray(value) || ArrayBuffer.isView(value)) || value.length !== length) fail(`${name} length`);
  for (const n of value) if (typeof n !== 'number' || !Number.isFinite(n) || Math.abs(n) > 1e6 ||
    (indices && (!Number.isInteger(n) || n < 0 || n >= max))) fail(`${name} value`);
  return indices ? Uint16Array.from(value) : Float32Array.from(value);
}
const vector = (v, n, key) => { numbers(v, n, key); return Array.from(v); };
const positive = (n, key) => { if (!Number.isFinite(n) || n <= 0 || n > 1e6) fail(key); return n; };
const scalar = (n, key) => vector([n], 1, key)[0];
function triangles(v, key) {
  if (!v || !v.length || v.length % 3 || v.length > 468 * 12) fail(key);
  const out = numbers(v, v.length, key, { indices: true });
  for (let i = 0; i < out.length; i += 3) if (out[i] === out[i + 1] || out[i] === out[i + 2] || out[i + 1] === out[i + 2]) fail(`${key} degenerate triangle`);
  return out;
}

/** Copy and validate before any live state is changed. Construction-time eye curves are deliberately omitted. */
export function hydrateRig(data) {
  if (!data || data.count !== 468 || !Array.isArray(data.channels) || data.channels.join('|') !== CH.join('|')) fail('rig/channel version');
  const n = data.count;
  if (!data.skin || data.skin.length !== canonical.skin.length || data.skin.some((v, i) => v !== canonical.skin[i])) fail('unsupported topology');
  if (!Array.isArray(data.deltas) || data.deltas.length !== CH.length) fail('deltas');
  const rig = {
    count: n, channels: CH.slice(), base: numbers(data.base, n * 3, 'base'),
    uvs: numbers(data.uvs, n * 2, 'uvs'), jawW: numbers(data.jawW, n, 'jawW'),
    pivot: vector(data.pivot, 3, 'pivot'), skin: triangles(data.skin, 'skin'),
    deltas: data.deltas.map((d, i) => numbers(d, n * 3, `delta ${i}`)), caps: {}, masks: {}, eyes: {},
  };
  for (const key of ['mouth', 'eyeR', 'eyeL']) {
    const cap = triangles(data.caps?.[key], `caps.${key}`);
    if (cap.length !== canonical.caps[key].length || cap.some((v, i) => v !== canonical.caps[key][i])) fail(`unsupported caps.${key}`);
    rig.caps[key] = Array.from(cap);
  }
  for (const key of ['edge', 'lips', 'brows', 'cheeks', 'socket']) rig.masks[key] = numbers(data.masks?.[key], n, `masks.${key}`);
  for (const side of ['R', 'L']) {
    const e = data.eyes?.[side];
    if (!e || !Array.isArray(e.lidSet) || !e.lidSet.length || e.lidSet.length > n || new Set(e.lidSet).size !== e.lidSet.length) fail(`eye ${side}`);
    rig.eyes[side] = { center: vector(e.center, 3, 'eye center'), radius: positive(e.radius, 'eye radius'),
      width: positive(e.width, 'eye width'), lidSet: new Set(numbers(e.lidSet, e.lidSet.length, 'lids', { indices: true })) };
  }
  const m = data.mouth;
  if (!m) fail('mouth');
  rig.mouth = { center: vector(m.center, 3, 'mouth center'), pivot: vector(m.pivot, 3, 'mouth pivot'),
    halfW: positive(m.halfW, 'mouth width'), zFront: scalar(m.zFront, 'mouth front'), seamY: scalar(m.seamY, 'mouth seam') };
  return rig;
}

/** JSON-safe explicit allowlist: never encode functions, closures or executable property names. */
export function rigData(rig) {
  if (rig.channels && (!Array.isArray(rig.channels) || rig.channels.join('|') !== CH.join('|'))) fail('rig/channel version');
  const data = { count: rig.count, channels: CH.slice() };
  for (const key of ['base', 'uvs', 'jawW', 'pivot', 'skin']) data[key] = Array.from(rig[key]);
  data.deltas = rig.deltas.map(d => Array.from(d));
  data.caps = Object.fromEntries(['mouth', 'eyeR', 'eyeL'].map(k => [k, Array.from(rig.caps[k])]));
  data.masks = Object.fromEntries(['edge', 'lips', 'brows', 'cheeks', 'socket'].map(k => [k, Array.from(rig.masks[k])]));
  data.eyes = Object.fromEntries(['R', 'L'].map(k => { const e = rig.eyes[k]; return [k,
    { center: Array.from(e.center), radius: e.radius, width: e.width, lidSet: Array.from(e.lidSet) }]; }));
  const m = rig.mouth;
  data.mouth = { center: Array.from(m.center), pivot: Array.from(m.pivot), halfW: m.halfW, zFront: m.zFront, seamY: m.seamY };
  hydrateRig(data);
  return data;
}
