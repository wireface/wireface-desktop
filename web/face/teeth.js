/*! Wireface | Required Notice: Copyright 2026 Compsmart (https://wireface.dev) | Wireface SDK Licence; free local use including commercial: https://wireface.dev/license/ */
// Each atlas contains two independent photographic rows. Rects and bite lines are
// normalized image coordinates (origin at the top left), calibrated per asset.
export const TEETH_STYLES = [
  ['normal', 'Normal'], ['braces', 'Braces'], ['vampire', 'Vampire'],
  ['goofy', 'Goofy'], ['sharp', 'Sharp'], ['turkey', 'Turkey teeth'],
];

export const TEETH_ASSETS = {
  normal: { upper: { rect: [0.060, 0.125, 0.940, 0.377], bite: 0.363, height: 1.18 },
    lower: { rect: [0.060, 0.615, 0.940, 0.865], bite: 0.640, height: 1.03 } },
  braces: { upper: { rect: [0.040, 0.110, 0.960, 0.389], bite: 0.377, height: 1.24 },
    lower: { rect: [0.040, 0.590, 0.960, 0.889], bite: 0.642, height: 1.10 } },
  vampire: { upper: { rect: [0.030, 0.105, 0.970, 0.415], bite: 0.333, height: 1.48 },
    lower: { rect: [0.030, 0.590, 0.970, 0.895], bite: 0.680, height: 1.05 } },
  goofy: { upper: { rect: [0.040, 0.120, 0.960, 0.400], bite: 0.350, height: 1.38 },
    lower: { rect: [0.040, 0.585, 0.965, 0.865], bite: 0.650, height: 1.06 } },
  sharp: { upper: { rect: [0.040, 0.110, 0.960, 0.390], bite: 0.350, height: 1.32 },
    lower: { rect: [0.040, 0.610, 0.960, 0.890], bite: 0.650, height: 1.14 } },
  turkey: { upper: { rect: [0.040, 0.120, 0.960, 0.395], bite: 0.380, height: 1.32 },
    lower: { rect: [0.040, 0.585, 0.960, 0.880], bite: 0.643, height: 1.12 } },
};

export function teethAsset(style) {
  const id = Object.hasOwn(TEETH_ASSETS, style) ? style : 'normal';
  return { id, url: new URL(`./teeth/${id}.png`, import.meta.url).href, ...TEETH_ASSETS[id] };
}

/** Curved photographic strips: position3, normal3, uv2, jawWeight1. */
export function teethGeometry(mouth, asset, width = 1, size = 1) {
  const out = [], angle = 1.25, depth = 1.5, segments = 40;
  const archW = mouth.halfW * 1.15 * width;
  for (const upper of [true, false]) {
    const row = upper ? asset.upper : asset.lower;
    const [x0, y0, x1, y1] = row.rect, scale = upper ? 1 : 0.94;
    const vertex = (t, v) => {
      const a = t * angle, nx = depth * Math.sin(a), nz = archW * Math.cos(a), len = Math.hypot(nx, nz);
      const x = Math.sin(a) * archW * scale;
      const y = mouth.seamY + ((upper ? -0.20 : 0.02) + (row.bite - v) / (y1 - y0) * row.height) * size;
      const z = mouth.zFront - 0.36 - (1 - Math.cos(a)) * depth * scale - (upper ? 0 : 0.1);
      // Frontal projection keeps central incisors from stretching along the arch.
      const u = x0 + (Math.sin(a) / Math.sin(angle) + 1) * 0.5 * (x1 - x0);
      return [x, y, z, nx / len, 0, nz / len, u, v, upper ? 0 : 1];
    };
    for (let i = 0; i < segments; i++) {
      const t0 = -1 + 2 * i / segments, t1 = -1 + 2 * (i + 1) / segments;
      const a = vertex(t0, y1), b = vertex(t1, y1), c = vertex(t1, y0), d = vertex(t0, y0);
      out.push(...a, ...b, ...c, ...a, ...c, ...d);
    }
  }
  return new Float32Array(out);
}
