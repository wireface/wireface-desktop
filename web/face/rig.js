/*! Wireface | Required Notice: Copyright 2026 Compsmart (https://wireface.dev) | Wireface SDK Licence; free local use including commercial: https://wireface.dev/license/ */
// Runtime channels and pose evaluation. Rig construction belongs to Creator.
export const CH = ['close', 'wide', 'round', 'upperUp', 'lowerDown', 'bite', 'smileL', 'smileR', 'frown',
  'blinkL', 'blinkR', 'squintL', 'squintR', 'eyeWideL', 'eyeWideR', 'browUpL', 'browUpR', 'browDown',
  'browInner', 'cheekPuff', 'sneer', 'pout'];
export const C = Object.fromEntries(CH.map((n, i) => [n, i]));
export const JAW_MAX = 0.24;            // radians at jaw = 1

// landmark rings (subject's left = +x; R = subject's right = viewer's left)
export const RING = {
  eyeR: [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246],
  eyeL: [263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466],
  lipsIn: [78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308, 415, 310, 311, 312, 13, 82, 81, 80, 191],
};

// --- per-frame deformation --------------------------------------------------------------------------------
export class Pose {
  constructor(rig) {
    this.rig = rig;
    this.pos = new Float32Array(rig.base.length);
    this.nrm = new Float32Array(rig.base.length);
    this.jawAngle = 0;
  }

  /** weights: Float32Array(CH.length); jaw: 0..1 */
  apply(weights, jaw) {
    const { base, deltas, jawW, pivot, count } = this.rig;
    const p = this.pos;
    p.set(base);
    for (let c = 0; c < deltas.length; c++) {
      const w = weights[c];
      if (Math.abs(w) < 1e-4) continue;
      const d = deltas[c];
      for (let k = 0; k < p.length; k++) p[k] += d[k] * w;
    }
    const a = jaw * JAW_MAX;
    this.jawAngle = a;
    if (a > 1e-4) {
      for (let i = 0; i < count; i++) {
        const w = jawW[i];
        if (w < 1e-3) continue;
        const t = a * w, cs = Math.cos(t), sn = Math.sin(t);
        const y = p[i * 3 + 1] - pivot[1], z = p[i * 3 + 2] - pivot[2];
        p[i * 3 + 1] = pivot[1] + y * cs - z * sn;
        p[i * 3 + 2] = pivot[2] + y * sn + z * cs;
      }
    }
    // eyelids never sink into the eyeballs
    for (const s of ['R', 'L']) {
      const e = this.rig.eyes[s];
      const rr = e.radius + 0.07025;
      for (const i of e.lidSet) {
        const dx = p[i * 3] - e.center[0], dy = p[i * 3 + 1] - e.center[1], dz = p[i * 3 + 2] - e.center[2];
        const d = Math.hypot(dx, dy, dz);
        if (d < rr && dz > 0) {
          const k = rr / d;
          p[i * 3] = e.center[0] + dx * k; p[i * 3 + 1] = e.center[1] + dy * k; p[i * 3 + 2] = e.center[2] + dz * k;
        }
      }
    }
    this.normals();
    return p;
  }

  normals() {
    const p = this.pos, n = this.nrm, tris = this.rig.skin;
    n.fill(0);
    for (let t = 0; t < tris.length; t += 3) {
      const a = tris[t] * 3, b = tris[t + 1] * 3, c = tris[t + 2] * 3;
      const ux = p[b] - p[a], uy = p[b + 1] - p[a + 1], uz = p[b + 2] - p[a + 2];
      const vx = p[c] - p[a], vy = p[c + 1] - p[a + 1], vz = p[c + 2] - p[a + 2];
      const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
      for (const o of [a, b, c]) { n[o] += nx; n[o + 1] += ny; n[o + 2] += nz; }
    }
    for (let k = 0; k < n.length; k += 3) {
      const l = Math.hypot(n[k], n[k + 1], n[k + 2]) || 1;
      n[k] /= l; n[k + 1] /= l; n[k + 2] /= l;
    }
  }
}
