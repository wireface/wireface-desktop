export function blendRigs(a, b, t, out = null) {
  if (!out) {
    out = {
      ...b, pivot: [0, 0, 0],
      base: new Float32Array(b.base.length), jawW: new Float32Array(b.jawW.length),
      deltas: b.deltas.map(d => new Float32Array(d.length)),
      masks: Object.fromEntries(Object.entries(b.masks).map(([k, v]) => [k, new Float32Array(v.length)])),
      mouth: { ...b.mouth, center: [0, 0, 0], pivot: [0, 0, 0] },
      eyes: {},
    };
    for (const s of ['R', 'L']) {
      // the lids kept outside the eyeballs: those of both shapes, so none sinks in on the way
      out.eyes[s] = { ...b.eyes[s], center: [0, 0, 0], lidSet: new Set([...a.eyes[s].lidSet, ...b.eyes[s].lidSet]) };
    }
  }
  const mix = (x, y, o) => { for (let k = 0; k < o.length; k++) o[k] = x[k] + (y[k] - x[k]) * t; return o; };
  const num = (x, y) => x + (y - x) * t;
  mix(a.base, b.base, out.base);
  mix(a.pivot, b.pivot, out.pivot);
  mix(a.mouth.pivot, b.mouth.pivot, out.mouth.pivot);
  mix(a.jawW, b.jawW, out.jawW);
  for (let c = 0; c < out.deltas.length; c++) mix(a.deltas[c], b.deltas[c], out.deltas[c]);
  for (const k of Object.keys(out.masks)) mix(a.masks[k], b.masks[k], out.masks[k]);
  for (const s of ['R', 'L']) {
    const e = out.eyes[s], ea = a.eyes[s], eb = b.eyes[s];
    mix(ea.center, eb.center, e.center);
    e.radius = num(ea.radius, eb.radius);
    e.width = num(ea.width, eb.width);
  }
  mix(a.mouth.center, b.mouth.center, out.mouth.center);
  for (const k of ['halfW', 'zFront', 'seamY']) out.mouth[k] = num(a.mouth[k], b.mouth[k]);
  return out;
}
