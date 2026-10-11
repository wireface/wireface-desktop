/*! Wireface | Required Notice: Copyright 2026 Compsmart (https://wireface.dev) | Wireface SDK Licence; free local use including commercial: https://wireface.dev/license/ */
// WebGL2 renderer for the face. Passes per frame:
//   1. skin depth pre-pass
//   2. the eye and mouth openings ("caps") into the stencil buffer
//   3. inner mouth (cavity, teeth, tongue) and eyeballs, only inside those openings
//   4. skin colour (hologram wireframe / sculpt / photo), premultiplied alpha
//   5. bloom (blurred copy added back) for the glow
// The canvas is transparent: in the floating window the desktop shows around the face.
import { teethAsset, teethGeometry } from './teeth.js';

// Decoded atlases are shared by previews and the floating face, including after a GPU reset.
const teethImages = new Map();
function loadTeethImage(url) {
  if (!teethImages.has(url)) {
    const pending = fetch(url).then(r => {
      if (!r.ok) throw new Error(`Teeth image: HTTP ${r.status}`);
      return r.blob();
    }).then(blob => createImageBitmap(blob, { premultiplyAlpha: 'premultiply', colorSpaceConversion: 'none' }));
    teethImages.set(url, pending);
    pending.catch(() => teethImages.delete(url));
  }
  return teethImages.get(url);
}

// --- small matrix helpers (column-major) ------------------------------------------------------------
export const M4 = {
  ident: () => new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]),
  mul(a, b) {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
    return o;
  },
  persp(fovy, aspect, near, far) {
    const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
    return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
  },
  translate: (x, y, z) => new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]),
  scale: s => new Float32Array([s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1]),
  rotX(a) { const c = Math.cos(a), s = Math.sin(a); return new Float32Array([1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]); },
  rotY(a) { const c = Math.cos(a), s = Math.sin(a); return new Float32Array([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]); },
  rotZ(a) { const c = Math.cos(a), s = Math.sin(a); return new Float32Array([c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]); },
  apply(m, p) {
    const x = p[0], y = p[1], z = p[2];
    const w = m[3] * x + m[7] * y + m[11] * z + m[15];
    return [(m[0] * x + m[4] * y + m[8] * z + m[12]) / w, (m[1] * x + m[5] * y + m[9] * z + m[13]) / w,
            (m[2] * x + m[6] * y + m[10] * z + m[14]) / w];
  },
};

export function hexRGB(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}

// --- shaders ------------------------------------------------------------------------------------------------
const SKIN_VS = `#version 300 es
in vec3 a_pos; in vec3 a_nrm; in vec3 a_bary; in vec2 a_photo; in vec4 a_mask; in float a_socket;
uniform mat4 u_proj, u_view, u_model;
out vec3 v_pos; out vec3 v_nrm; out vec3 v_bary; out vec2 v_photo; out vec4 v_mask; out float v_socket; out float v_y;
invariant gl_Position;
void main() {
  v_pos = (u_model * vec4(a_pos, 1.0)).xyz; v_nrm = mat3(u_model) * a_nrm; v_bary = a_bary; v_photo = a_photo; v_mask = a_mask; v_socket = a_socket;
  v_y = a_pos.y;
  gl_Position = u_proj * u_view * u_model * vec4(a_pos, 1.0);   // same expression as CAP_VS: equal depths
}`;

const SKIN_FS = `#version 300 es
precision highp float;
in vec3 v_pos; in vec3 v_nrm; in vec3 v_bary; in vec2 v_photo; in vec4 v_mask; in float v_socket; in float v_y;
uniform int u_style;
uniform vec3 u_primary, u_secondary, u_skin, u_lips, u_eye;
uniform float u_wire, u_fill, u_scan, u_points, u_edgeFade, u_time, u_glow, u_dim, u_talk;
uniform float u_sweepY, u_sweeping;         // the sculpt/photo skin covers the hologram above u_sweepY (model y)
uniform float u_blend, u_skinIn;            // and there cross-fades to it: the wireframe's lines go as 1 - blend, the
                                            // skin (in place of the hologram's fill) comes in as skinIn, a little ahead
uniform float u_glowWire, u_glowSkin;       // how much of each goes into the bloom
uniform sampler2D u_photoTex;
layout(location = 0) out vec4 o;
layout(location = 1) out vec4 o2;           // what glows: the bloom's source
float lineAt(vec3 b, float w) { vec3 d = fwidth(b); vec3 a = smoothstep(vec3(0.0), d * w, b); return 1.0 - min(min(a.x, a.y), a.z); }
void main() {
  vec3 n = normalize(v_nrm);
  vec3 V = normalize(u_eye - v_pos);
  if (!gl_FrontFacing) n = -n;
  float ndv = clamp(dot(n, V), 0.0, 1.0);
  float fres = pow(1.0 - ndv, 2.2023);
  float edge = u_edgeFade > 0.001 ? smoothstep(0.0, u_edgeFade, v_mask.x) : 1.0;
  // derivatives outside any branch that varies across the face
  float line = lineAt(v_bary, 1.0975), halo = lineAt(v_bary, 3.5062), thin = lineAt(v_bary, 1.0);
  float solid = u_style == 0 ? 0.0 : smoothstep(u_sweepY - 0.25, u_sweepY + 0.25, v_y);
  float skin = solid * u_skinIn, lines = 1.0 - solid * u_blend;
  vec3 col = vec3(0.0), hfill = vec3(0.0), hline = vec3(0.0); float fa = 0.0, la = 0.0;
  if (skin < 0.999 || lines > 0.001) {
    float scan = 0.5 + 0.5 * sin(gl_FragCoord.y * 1.6013 - u_time * 5.0);
    float sweep = smoothstep(0.0, 0.08, fract(v_pos.y * 0.04 - u_time * 0.12)) * smoothstep(0.16, 0.08, fract(v_pos.y * 0.04 - u_time * 0.12));
    float fill = u_fill * (0.30 + 0.70 * fres) * (1.0 - u_scan * 0.5482 * scan);
    float vtx = smoothstep(0.90, 0.9703, max(max(v_bary.x, v_bary.y), v_bary.z));
    float wire = u_wire * (0.88 + 0.12009 * u_talk);
    // the haze (fill, rim, the lines' halo) gives way to the skin; the lines themselves fade more slowly
    hfill = (u_primary * (fill * 0.9 + halo * wire * 0.22083 * u_glow) + u_secondary * fres * u_fill * 0.8036) * u_dim;
    hline = u_primary * (line * wire * 1.2455 + sweep * 0.18 * u_scan + vtx * u_points * 1.6047) * u_dim;
    fa = fill * 0.8487 + halo * wire * 0.12;
    la = line * wire * 0.8971 + vtx * u_points;
  }
  if (skin > 0.001) {
    vec3 L1 = normalize(vec3(-0.45, 0.5515, 0.75)), L2 = normalize(vec3(0.65, 0.05, 0.55));
    float wrap = 0.35091;
    float d1 = clamp((dot(n, L1) + wrap) / (1.0 + wrap), 0.0, 1.0);
    float d2 = clamp((dot(n, L2) + wrap) / (1.0 + wrap), 0.0, 1.0);
    vec3 base;
    if (u_style == 2) {
      base = texture(u_photoTex, v_photo).rgb;
      col = base * (0.62 + 0.42135 * d1 + 0.12 * d2);
    } else {
      base = mix(u_skin, u_lips, v_mask.y * 0.8478);
      base = mix(base, base * vec3(0.4777, 0.40, 0.38), v_mask.z * 0.65);
      base = mix(base, base * vec3(1.0548, 0.86, 0.86), v_mask.w * 0.3);
      base *= 1.0 - v_socket * 0.21913;
      col = base * (d1 * 0.7167 * vec3(1.0, 0.97, 0.93) + d2 * 0.22 * vec3(0.85, 0.9, 1.0) + 0.12974);
      col += vec3(0.6, 0.18007, 0.12) * base * d1 * pow(1.0 - d1, 1.5) * 0.6;
    }
    vec3 H = normalize(L1 + V);
    col += pow(clamp(dot(n, H), 0.0, 1.0), 36.016) * (0.08 + 0.21992 * v_mask.y) * vec3(1.0, 0.97, 0.94);
    float photo = u_style == 2 ? 1.0 : 0.0;
    col += mix(u_secondary, vec3(1.0, 0.9, 0.8), 0.5) * fres * 0.17914 * u_glow * (1.0 - 0.75 * photo);
    col += u_primary * thin * u_wire * 0.22 * (1.0 - photo);
    col *= u_dim;
  }
  // with all of the skin (blend 1) this is mix(hologram, skin, solid)
  float a = min(1.0, skin + fa * (1.0 - skin) + la * lines);
  vec4 glow = (vec4(hfill, fa) * (1.0 - skin) + vec4(hline, la) * lines) * u_glowWire + vec4(col, 1.0) * skin * u_glowSkin;
  glow.a = min(glow.a, max(u_glowWire, u_glowSkin));
  col = col * skin + hfill * (1.0 - skin) + hline * lines;
  // the sweep's leading edge: a bright band with the wireframe lit up in it
  float band = u_sweeping * exp(-pow((v_y - u_sweepY) / 0.4503, 2.0));
  vec3 lit = u_primary * band * (0.5 + 1.6 * line) * u_dim;
  col += lit;
  a = max(a, band * 0.85);
  glow = vec4(glow.rgb + lit * u_glowWire, max(glow.a, band * 0.85 * u_glowWire));
  o = vec4(col * edge, a * edge);
  o2 = glow * edge;
}`;

const CAP_VS = `#version 300 es
in vec3 a_pos; uniform mat4 u_proj, u_view, u_model;
invariant gl_Position;
void main() { gl_Position = u_proj * u_view * u_model * vec4(a_pos, 1.0); }`;
const CAP_FS = `#version 300 es
precision mediump float; out vec4 o; void main() { o = vec4(0.0); }`;

const EYE_VS = `#version 300 es
in vec3 a_pos;
uniform mat4 u_proj, u_view, u_eyeModel;
out vec3 v_local; out vec3 v_world; out vec3 v_nrm;
void main() {
  vec4 w = u_eyeModel * vec4(a_pos, 1.0);
  v_local = a_pos; v_world = w.xyz; v_nrm = normalize(mat3(u_eyeModel) * a_pos);
  gl_Position = u_proj * u_view * w;
}`;
const EYE_FS = `#version 300 es
precision highp float;
in vec3 v_local; in vec3 v_world; in vec3 v_nrm;
uniform vec3 u_iris, u_primary, u_eye; uniform float u_pupil, u_dim;
uniform float u_blend, u_glowWire, u_glowSkin;   // hologram (0) to real (1) eyes; how much of each glows
uniform float u_spin, u_time;                    // waiting: how much of the glow and spinner shows (0..1); the clock
layout(location = 0) out vec4 o;
layout(location = 1) out vec4 o2;
void main() {
  vec3 p = normalize(v_local);
  float ang = acos(clamp(p.z, -1.0, 1.0));
  float irisR = 0.5175, pupilR = u_pupil;
  float phi = atan(p.y, p.x);
  float fib = 0.5 + 0.5 * sin(phi * 38.0 + sin(phi * 11.0) * 2.5042);
  float inIris = 1.0 - smoothstep(irisR - 0.015, irisR + 0.015, ang);
  float inPupil = 1.0 - smoothstep(pupilR - 0.015, pupilR + 0.015, ang);
  vec3 n = normalize(v_nrm), V = normalize(u_eye - v_world);
  float spec = pow(max(dot(n, normalize(normalize(vec3(-0.35, 0.5, 1.0)) + V)), 0.0), 139.65);
  float lid = smoothstep(-0.1, 0.55, p.y);                     // upper lid shadow
  vec3 holo = vec3(0.0), real = vec3(0.0);
  if (u_blend < 0.999) {
    vec3 irisC = u_primary * (0.55 + 0.9 * smoothstep(pupilR, irisR, ang)) * (0.8 + 0.2 * fib);
    holo = mix(u_primary * 0.06, irisC, inIris);
    holo = mix(holo, u_primary * 0.03, inPupil);
    holo += spec * mix(vec3(1.0), u_primary, 0.4) * 0.8;
  }
  if (u_blend > 0.001) {
    vec3 sclera = vec3(0.92, 0.9085, 0.88) * (0.72 + 0.28 * p.z);
    sclera = mix(sclera, vec3(0.86, 0.62, 0.6), smoothstep(0.9, 1.5, ang) * 0.35);
    vec3 irisC = u_iris * (0.45 + 0.55 * smoothstep(pupilR, irisR, ang)) * (0.8 + 0.2 * fib);
    irisC = mix(irisC, irisC * 0.3, smoothstep(irisR - 0.09034, irisR, ang));
    real = mix(sclera, irisC, inIris);
    real = mix(real, vec3(0.015), inPupil);
    real *= 1.0 - 0.35115 * lid;
    real += spec * 0.85;
  }
  // waiting: the iris glows softly, and a spinner turns in the dark pupil: a faint track, and on it a short arc,
  // white-hot at its head and fading to its tail (whatever the style, and it always glows)
  vec3 wait = vec3(0.0);
  if (u_spin > 0.001) {
    vec3 hot = mix(u_primary, vec3(1.0), 0.6);
    float r = ang / max(pupilR, 0.05);                             // 0 at the pupil's centre, 1 at its edge
    float ring = smoothstep(0.3, 0.43, r) * (1.0 - smoothstep(0.82, 0.95, r));
    float a = fract(phi / 6.2831853 + u_time * 1.2);               // along the turn: the head is at 1
    float arc = pow(smoothstep(0.55, 1.0, a), 2.0) * (1.0 - smoothstep(0.985, 1.0, a));
    float iris = inIris * (1.0 - inPupil) * (0.4 + 0.12 * sin(u_time * 3.0));
    wait = (hot * ring * (0.06 + 2.6 * arc) + u_primary * iris) * u_spin;
  }
  o = vec4((mix(holo, real, u_blend) + wait) * u_dim, 1.0);
  float gw = (1.0 - u_blend) * u_glowWire, gs = u_blend * u_glowSkin;
  o2 = vec4((holo * gw + real * gs + wait * 2.5) * u_dim, min(1.0, gw + gs + u_spin));   // a halo round the eyes
}`;

const MOUTH_VS = `#version 300 es
in vec3 a_pos; in vec3 a_nrm; in float a_part; in float a_aux; in float a_jaw;
uniform mat4 u_proj, u_view, u_model; uniform float u_jaw, u_zFront; uniform vec3 u_pivot; uniform vec2 u_tongue;
out vec3 v_nrm; out float v_part; out float v_aux; out float v_depth;
vec3 rotJaw(vec3 p, float a) {
  float c = cos(a), s = sin(a); vec3 q = p - u_pivot;
  return u_pivot + vec3(q.x, q.y * c - q.z * s, q.y * s + q.z * c);
}
void main() {
  vec3 p = a_pos;
  if (a_part > 1.5 && a_part < 2.5) p += vec3(0.0, u_tongue.x, u_tongue.y) * smoothstep(-0.2, 1.0, a_aux);
  v_depth = u_zFront - p.z;                                    // how far behind the lips, before the jaw moves it
  float a = u_jaw * a_jaw;
  p = rotJaw(p, a);
  vec3 n = rotJaw(a_nrm + u_pivot, a) - u_pivot;
  v_nrm = mat3(u_model) * n; v_part = a_part; v_aux = a_aux;
  gl_Position = u_proj * u_view * u_model * vec4(p, 1.0);
}`;
const MOUTH_FS = `#version 300 es
precision highp float;
in vec3 v_nrm; in float v_part; in float v_aux; in float v_depth;
uniform vec3 u_primary, u_mouth; uniform float u_dim;
uniform float u_blend, u_glowWire, u_glowSkin;   // hologram (0) to real (1) colours; how much of each glows
uniform bool u_hideDentition;
layout(location = 0) out vec4 o;
layout(location = 1) out vec4 o2;
void main() {
  if (u_hideDentition && ((v_part > 0.5 && v_part < 1.5) || v_part > 2.5)) discard;
  vec3 n = normalize(v_nrm);
  float light = 0.6783 + 0.32 * clamp(dot(n, normalize(vec3(-0.2, 0.4, 1.0))), 0.0, 1.0);
  float depth = clamp(1.0 - v_depth * 0.31921, 0.18, 1.0);
  vec3 col;
  if (v_part < 0.5) {                                          // cavity: the set colour near the lips, darker down the throat
    col = u_mouth * mix(1.2, 0.55, smoothstep(0.15, 1.0, v_aux));
    depth = 1.0;
  } else if (v_part < 1.5) {
    float gap = smoothstep(0.0, 0.25, abs(fract(v_aux * 5.0) - 0.5) * 2.0);
    col = vec3(1.0, 0.97, 0.92) * (0.62 + 0.38 * (1.0 - gap * 0.6));
  } else if (v_part < 2.5) {                                   // tongue: kept lighter than the walls so it reads
    col = mix(vec3(0.72, 0.30981, 0.33), u_mouth, 0.15);
    depth = mix(depth, 1.0, 0.5);
  } else col =mix(u_mouth, vec3(0.80, 0.38094, 0.40), 0.55) * (1.0 - 0.3 * v_aux);   // gums, darker away from the teeth
  col *= light * depth;
  vec3 holo = u_primary * dot(col, vec3(0.35)) * 0.9;
  o = vec4(mix(holo, col, u_blend) * u_dim, 1.0);
  float gw = (1.0 - u_blend) * u_glowWire, gs = u_blend * u_glowSkin;
  o2 = vec4((holo * gw + col * gs) * u_dim, gw + gs);
}`;

const TEETH_VS = `#version 300 es
in vec3 a_pos; in vec3 a_nrm; in vec2 a_uv; in float a_jaw;
uniform mat4 u_proj, u_view, u_model;
uniform float u_jaw, u_zFront; uniform vec3 u_pivot;
out vec2 v_uv; out vec3 v_nrm; out float v_depth;
void main() {
  float c = cos(u_jaw * a_jaw), s = sin(u_jaw * a_jaw);
  mat3 jaw = mat3(1.0, 0.0, 0.0, 0.0, c, s, 0.0, -s, c);
  vec3 p = u_pivot + jaw * (a_pos - u_pivot);
  v_uv = a_uv; v_nrm = mat3(u_model) * jaw * a_nrm;
  v_depth = u_zFront - a_pos.z;
  gl_Position = u_proj * u_view * u_model * vec4(p, 1.0);
}`;
const TEETH_FS = `#version 300 es
precision highp float;
in vec2 v_uv; in vec3 v_nrm; in float v_depth;
uniform sampler2D u_teeth; uniform float u_dim; uniform vec3 u_primary;
uniform float u_blend, u_glowWire, u_glowSkin;   // hologram (0) to real (1) colours; how much of each glows
layout(location = 0) out vec4 o;
layout(location = 1) out vec4 o2;
void main() {
  vec4 tex = texture(u_teeth, v_uv);  // premultiplied before filtering/mipmap generation
  if (tex.a < 0.10) discard;         // gaps must not write depth or cover the tongue
  float light = 0.80 + 0.20 * max(dot(normalize(v_nrm), normalize(vec3(-0.2, 0.4, 1.0))), 0.0);
  float shade = clamp(1.0 - v_depth * 0.23951, 0.28, 1.0);
  vec3 real = tex.rgb * light * shade, holo = u_primary * dot(real, vec3(0.35)) * 0.9;
  o = vec4(mix(holo, real, u_blend) * u_dim, tex.a);
  float gw = (1.0 - u_blend) * u_glowWire, gs = u_blend * u_glowSkin;
  o2 = vec4((holo * gw + real * gs) * u_dim, tex.a * (gw + gs));
}`;

const QUAD_VS = `#version 300 es
in vec2 a_xy; out vec2 v_uv;
void main() { v_uv = a_xy * 0.5 + 0.5; gl_Position = vec4(a_xy, 0.0, 1.0); }`;
const BLUR_FS = `#version 300 es
precision mediump float;
in vec2 v_uv; uniform sampler2D u_tex; uniform vec2 u_dir; out vec4 o;
void main() {
  vec4 s = texture(u_tex, v_uv) * 0.2270270270;
  s += (texture(u_tex, v_uv + u_dir * 1.3846153846) + texture(u_tex, v_uv - u_dir * 1.3846153846)) * 0.3162162162;
  s += (texture(u_tex, v_uv + u_dir * 3.2307692308) + texture(u_tex, v_uv - u_dir * 3.2307692308)) * 0.0702702703;
  o = s;
}`;
// The scene plus its glow. u_vanish (0 = all there, 1 = gone) plays the face going away; coming back is the same
// played backwards. First a glitch (slices jump sideways, the colours split, the picture flickers), then a ragged
// glowing edge burns the face away from below the chin to above the head, throwing off sparks that drift up.
// u_box: the face's centre and half size (uv), u_res: the canvas in css px, u_tint: the primary colour.
const COMPOSE_FS = `#version 300 es
precision highp float;
in vec2 v_uv; uniform sampler2D u_scene, u_bloom; uniform float u_glow;
uniform float u_vanish, u_time; uniform vec4 u_box; uniform vec2 u_res; uniform vec3 u_tint;
out vec4 o;
float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), f.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y);
}
vec4 scene(vec2 uv) {
  vec4 s = texture(u_scene, uv), b = texture(u_bloom, uv) * u_glow;
  return vec4(s.rgb + b.rgb * 1.1031, clamp(s.a + b.a * 0.75, 0.0, 1.0));
}
void main() {
  float p = u_vanish;
  if (p <= 0.0) { o = scene(v_uv); return; }
  if (p >= 1.0) { o = vec4(0.0); return; }
  vec2 px = v_uv * u_res, c = u_box.xy, hs = u_box.zw;
  float env = smoothstep(0.0, 0.12, p) * (1.0 - smoothstep(0.88, 1.0, p));
  // 1. glitch, strongest at the start
  float g = sin(clamp(p / 0.5, 0.0, 1.0) * 3.14159);
  float tick = floor(u_time * 18.0);
  float band = floor(px.y / (3.0 + 10.0 * hash(vec2(tick, 0.5))));
  float jump = step(0.78, hash(vec2(band, tick))) * (hash(vec2(tick, band)) - 0.5);
  vec2 uv = v_uv + vec2(jump * hs.x * 0.5 * g, 0.0);
  float ca = g * 3.5 / u_res.x;
  vec4 sr = scene(uv + vec2(ca, 0.0)), sg = scene(uv), sb = scene(uv - vec2(ca, 0.0));
  vec4 s = vec4(sr.r, sg.g, sb.b, max(sg.a, max(sr.a, sb.a)));
  s *= 1.0 - g * 0.4 * step(0.72, hash(vec2(tick, 9.1)));
  s += vec4(u_tint, 1.0) * step(0.975, hash(vec2(floor(px.y / 2.0), tick))) * g * s.a * 0.7;
  // 2. the burn: below the edge the face is gone, just above it glows white-hot fading to the tint
  float front = mix(c.y - hs.y * 1.3, c.y + hs.y * 1.3, smoothstep(0.1, 0.9, p));
  float n = noise(px / 9.0) * 0.65 + noise(px / 3.0) * 0.35;
  float d = (v_uv.y - front) / (hs.y * 0.3) + (n - 0.5) * 1.4;
  float keep = smoothstep(0.0, 0.1, d);
  float rim = keep * (1.0 - smoothstep(0.1, 0.75, d)) * env;
  vec3 hot = mix(u_tint, vec3(1.0), 1.0 - smoothstep(0.1, 0.35, d));
  vec4 col = s * keep + vec4(hot * 1.3, 1.0) * rim * s.a;
  // 3. sparks: a field of points drifting up, alive near the edge in the columns the face fills
  vec2 q = (px - vec2(0.0, u_time * 70.0)) / 6.0;
  vec2 id = floor(q), f = fract(q) - 0.5;
  float h = hash(id);
  vec2 off = vec2(hash(id + 3.7), hash(id + 9.2)) - 0.5;
  float spark = step(0.8, h) * smoothstep(0.34, 0.0, length(f - off * 0.5)) * (0.55 + 0.45 * sin(u_time * 14.0 + h * 40.0));
  float above = v_uv.y - front;
  float zone = above > 0.0 ? exp(-above / (hs.y * 0.75)) : exp(above / (hs.y * 0.1));
  float there = 0.0;
  for (int k = -2; k <= 2; k++) there += texture(u_scene, vec2(v_uv.x + float(k) * hs.x * 0.06, clamp(front + hs.y * 0.08, 0.0, 1.0))).a;
  col += vec4(mix(u_tint, vec3(1.0), 0.4) * 1.6, 1.0) * spark * zone * clamp(there / 3.0, 0.0, 1.0) * env;
  o = vec4(col.rgb, clamp(col.a, 0.0, 1.0));
}`;
const COPY_FS = `#version 300 es
precision mediump float;
in vec2 v_uv; uniform sampler2D u_tex; out vec4 o; void main() { o = texture(u_tex, v_uv); }`;

// --- geometry builders -------------------------------------------------------------------------------------
function sphere(segU = 32, segV = 20) {
  const v = [];
  const pt = (i, j) => {
    const th = (j / segV) * Math.PI, ph = (i / segU) * Math.PI * 2;
    return [Math.sin(th) * Math.cos(ph), Math.sin(th) * Math.sin(ph), Math.cos(th)];   // +z = gaze axis pole
  };
  for (let j = 0; j < segV; j++) for (let i = 0; i < segU; i++) {
    const a = pt(i, j), b = pt(i + 1, j), c = pt(i + 1, j + 1), d = pt(i, j + 1);
    v.push(...a, ...d, ...c, ...a, ...c, ...b);
  }
  return new Float32Array(v);
}

/** Inner mouth: cavity, gums, upper/lower teeth arches and tongue. Vertex: pos3 nrm3 part aux jaw.
 *  Parts: 0 cavity, 1 teeth, 2 tongue, 3 gums. width/size: the teeth settings (1 = default). */
function mouthGeometry(mouth, { width = 1, size = 1 } = {}) {
  const { halfW, zFront, seamY } = mouth;
  const out = [];
  const push = (p, n, part, aux, jaw) => out.push(p[0], p[1], p[2], n[0], n[1], n[2], part, aux, jaw);
  const quad = (a, b, c, d, n, part, jaw, aux = [0, 0, 0, 0]) => {
    const [na, nb, nc, nd] = Array.isArray(n[0]) ? n : [n, n, n, n];
    const [ja, jb, jc, jd] = Array.isArray(jaw) ? jaw : [jaw, jaw, jaw, jaw];
    push(a, na, part, aux[0], ja); push(b, nb, part, aux[1], jb); push(c, nc, part, aux[2], jc);
    push(a, na, part, aux[0], ja); push(c, nc, part, aux[2], jc); push(d, nd, part, aux[3], jd);
  };
  // teeth arch: x = archW sin(a), z = -D (1 - cos(a)); at width 1 it reaches the corners of a wide-open mouth
  const archW = halfW * 1.15 * width, D = 1.5, ANG = 1.25;
  const arch = (t, s) => {
    const a = t * ANG, nx = D * Math.sin(a), nz = archW * Math.cos(a), l = Math.hypot(nx, nz);
    return { x: Math.sin(a) * archW * s, z: zFront - 0.32 - (1 - Math.cos(a)) * D * s, nx: nx / l, nz: nz / l };
  };
  // cavity: the back half of an ellipsoid behind the lips, seen from inside. The roof stays with the skull,
  // the floor drops with the jaw and the cheeks stretch between them; it widens to hold wide teeth.
  // aux = 0 at the lips .. 1 down the throat
  const rx = Math.max(halfW * 1.12, archW * Math.sin(ANG) * 1.3), ryUp = 1.35, ryLo = 1.7, rz = 2.8998, zc = zFront - 0.15;
  const NA = 36, ND = 10;
  const cav = (i, j) => {
    const th = (i / NA) * Math.PI * 2, ph = (j / ND) * Math.PI / 2;     // th: 0 roof, PI floor; ph: 0 rim, PI/2 throat
    const c = Math.cos(th), r = Math.cos(ph), ry = c > 0 ? ryUp : ryLo;
    const p = [Math.sin(th) * rx * r, seamY + c * ry * r, zc - Math.sin(ph) * rz];
    const g = [p[0] / (rx * rx), (p[1] - seamY) / (ry * ry), (p[2] - zc) / (rz * rz)];
    const l = Math.hypot(...g) || 1;
    const t = Math.min(1, Math.max(0, (0.4483 - c) / 0.8973));
    return { p, n: g.map(v => -v / l), jaw: t * t * (3 - 2 * t), aux: Math.sin(ph) };
  };
  for (let i = 0; i < NA; i++) for (let j = 0; j < ND; j++) {
    const v = [cav(i, j), cav(i + 1, j), cav(i + 1, j + 1), cav(i, j + 1)];
    quad(...v.map(q => q.p), v.map(q => q.n), 0, v.map(q => q.jaw), v.map(q => q.aux));
  }
  // teeth, with gums above the uppers and below the lowers; size scales their height about the seam
  const NT = 24, GUM = 0.45;
  for (const upper of [true, false]) {
    const s = upper ? 1 : 0.9374;
    const yTop = seamY + (upper ? 0.55 : 0.04) * size, yBot = seamY - (upper ? 0.24 : 0.55) * size;
    const zo = upper ? 0 : -0.1;
    const yGum = upper ? yTop : yBot, yGumEnd = yGum + (upper ? GUM : -GUM);
    for (let i = 0; i < NT; i++) {
      const t0 = -1 + (2 * i) / NT, t1 = -1 + (2 * (i + 1)) / NT;
      const a = arch(t0, s), b = arch(t1, s);
      const n = [(a.nx + b.nx) / 2, 0, (a.nz + b.nz) / 2];
      const u0 = (t0 + 1) / 2 * 2.4, u1 = (t1 + 1) / 2 * 2.4;
      quad([a.x, yBot, a.z + zo], [b.x, yBot, b.z + zo], [b.x, yTop, b.z + zo], [a.x, yTop, a.z + zo], n, 1, upper ? 0 : 1, [u0, u1, u1, u0]);
      // gum: just in front of the tooth roots, leaning back towards the palate / the floor
      const out0 = 0.05, back = -0.25;
      const ga = [a.x + a.nx * out0, yGum, a.z + zo + a.nz * out0], gb = [b.x + b.nx * out0, yGum, b.z + zo + b.nz * out0];
      const ge = (q, k) => [q.x + q.nx * k, yGumEnd, q.z + zo + q.nz * k];
      quad(ga, gb, ge(b, back), ge(a, back), n, 3, upper ? 0 : 1, [0, 0, 1, 1]);
    }
  }
  // tongue: a flattened dome resting on the jaw; aux = 0 root .. 1 tip
  const NU = 10, NV = 8, L = 2.0, tipZ = zFront - 0.75;
  const tp = (u, v) => {
    const w = archW * 0.5406 * (0.55 + 0.45 * Math.sin(Math.min(1, 0.3 + u) * Math.PI * 0.6));    // fits inside the lower teeth
    const x = (v - 0.5) * 2 * w;
    const y = seamY - 0.72 + 0.32 * Math.sin(v * Math.PI) * (0.6 + 0.4 * Math.sin(u * Math.PI));
    return [x, y, tipZ - L + u * L];
  };
  for (let i = 0; i < NU; i++) for (let j = 0; j < NV; j++) {
    const u0 = i / NU, u1 = (i + 1) / NU, v0 = j / NV, v1 = (j + 1) / NV;
    quad(tp(u0, v0), tp(u0, v1), tp(u1, v1), tp(u1, v0), [0, 1, 0.3], 2, 1, [u0, u0, u1, u1]);
  }
  return new Float32Array(out);
}

// --- renderer ------------------------------------------------------------------------------------------------
export class Renderer {
  constructor(canvas, rig) {
    const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: false, stencil: true, depth: true });
    if (!gl) throw new Error('WebGL2 is not available');
    this.gl = gl;
    this.canvas = canvas;
    this.rig = rig;
    this.progs = {
      skin: this.program(SKIN_VS, SKIN_FS), cap: this.program(CAP_VS, CAP_FS), eye: this.program(EYE_VS, EYE_FS),
      mouth: this.program(MOUTH_VS, MOUTH_FS), blur: this.program(QUAD_VS, BLUR_FS),
      teeth: this.program(TEETH_VS, TEETH_FS),
      compose: this.program(QUAD_VS, COMPOSE_FS), copy: this.program(QUAD_VS, COPY_FS),
    };
    this.samples = Math.min(4, gl.getParameter(gl.MAX_SAMPLES));
    this.buildStatic();
    this.size = [0, 0];
    this.photoTex = null;
    this.photoIris = null;       // the iris colour of the photo's own eyes (so brown eyes stay brown)
    this.teethTextures = new Map();
    this.teethGeometryKey = null;
    this.setTeethStyle('normal');
  }

  program(vs, fs) {
    const gl = this.gl;
    const sh = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const loc = {};
    const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) { const u = gl.getActiveUniform(p, i); loc[u.name] = gl.getUniformLocation(p, u.name); }
    const attrs = {};
    const na = gl.getProgramParameter(p, gl.ACTIVE_ATTRIBUTES);
    for (let i = 0; i < na; i++) { const a = gl.getActiveAttrib(p, i); attrs[a.name] = gl.getAttribLocation(p, a.name); }
    return { p, u: loc, a: attrs };
  }

  buffer(data, usage) {
    const gl = this.gl;
    const b = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, data, usage || gl.STATIC_DRAW);
    return b;
  }

  attr(prog, name, buf, size, stride = 0, offset = 0) {
    const gl = this.gl;
    const loc = prog.a[name];
    if (loc === undefined || loc < 0) return;
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, offset);
  }

  buildStatic() {
    const gl = this.gl, rig = this.rig;
    const skin = rig.skin;
    const nv = skin.length;
    this.skinIdx = skin;
    this.skinPos = new Float32Array(nv * 3);
    this.skinNrm = new Float32Array(nv * 3);
    const bary = new Float32Array(nv * 3), mask = new Float32Array(nv * 4), socket = new Float32Array(nv);
    this.photoUV = new Float32Array(nv * 2);
    for (let k = 0; k < nv; k++) {
      const v = skin[k];
      bary[k * 3 + (k % 3)] = 1;
      mask[k * 4] = rig.masks.edge[v]; mask[k * 4 + 1] = rig.masks.lips[v];
      mask[k * 4 + 2] = rig.masks.brows[v]; mask[k * 4 + 3] = rig.masks.cheeks[v];
      socket[k] = rig.masks.socket[v];
      this.photoUV[k * 2] = rig.uvs[v * 2]; this.photoUV[k * 2 + 1] = rig.uvs[v * 2 + 1];
    }
    this.buf = {
      pos: this.buffer(this.skinPos, gl.DYNAMIC_DRAW), nrm: this.buffer(this.skinNrm, gl.DYNAMIC_DRAW),
      bary: this.buffer(bary), mask: this.buffer(mask), socket: this.buffer(socket), photo: this.buffer(this.photoUV, gl.DYNAMIC_DRAW),
    };
    const skinP = this.progs.skin;
    this.vaoSkin = gl.createVertexArray();
    gl.bindVertexArray(this.vaoSkin);
    this.attr(skinP, 'a_pos', this.buf.pos, 3); this.attr(skinP, 'a_nrm', this.buf.nrm, 3);
    this.attr(skinP, 'a_bary', this.buf.bary, 3); this.attr(skinP, 'a_mask', this.buf.mask, 4);
    this.attr(skinP, 'a_socket', this.buf.socket, 1); this.attr(skinP, 'a_photo', this.buf.photo, 2);
    this.vaoDepth = gl.createVertexArray();
    gl.bindVertexArray(this.vaoDepth);
    this.attr(this.progs.cap, 'a_pos', this.buf.pos, 3);
    // caps (openings)
    this.capIdx = { mouth: rig.caps.mouth, eyes: [...rig.caps.eyeR, ...rig.caps.eyeL] };
    this.capPos = { mouth: new Float32Array(this.capIdx.mouth.length * 3), eyes: new Float32Array(this.capIdx.eyes.length * 3) };
    this.capBuf = {};
    this.vaoCap = {};
    for (const k of ['mouth', 'eyes']) {
      this.capBuf[k] = this.buffer(this.capPos[k], gl.DYNAMIC_DRAW);
      this.vaoCap[k] = gl.createVertexArray();
      gl.bindVertexArray(this.vaoCap[k]);
      this.attr(this.progs.cap, 'a_pos', this.capBuf[k], 3);
    }
    // eyes
    const sph = sphere();
    this.eyeCount = sph.length / 3;
    this.vaoEye = gl.createVertexArray();
    gl.bindVertexArray(this.vaoEye);
    this.eyeBuf = this.buffer(sph);
    this.attr(this.progs.eye, 'a_pos', this.eyeBuf, 3);
    // mouth interior (rebuilt when the teeth settings change)
    const mb = this.mouthBuf = gl.createBuffer();
    this.mouthKey = null;
    this.setTeeth(1, 1);
    const mp = this.progs.mouth;
    this.vaoMouth = gl.createVertexArray();
    gl.bindVertexArray(this.vaoMouth);
    this.attr(mp, 'a_pos', mb, 3, 36, 0); this.attr(mp, 'a_nrm', mb, 3, 36, 12);
    this.attr(mp, 'a_part', mb, 1, 36, 24); this.attr(mp, 'a_aux', mb, 1, 36, 28); this.attr(mp, 'a_jaw', mb, 1, 36, 32);
    this.teethBuf = gl.createBuffer();
    this.vaoTeeth = gl.createVertexArray();
    gl.bindVertexArray(this.vaoTeeth);
    const tp = this.progs.teeth;
    this.attr(tp, 'a_pos', this.teethBuf, 3, 36, 0); this.attr(tp, 'a_nrm', this.teethBuf, 3, 36, 12);
    this.attr(tp, 'a_uv', this.teethBuf, 2, 36, 24); this.attr(tp, 'a_jaw', this.teethBuf, 1, 36, 32);
    // fullscreen quad
    this.vaoQuad = gl.createVertexArray();
    gl.bindVertexArray(this.vaoQuad);
    const qb = this.quadBuf = this.buffer(new Float32Array([-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1]));
    for (const k of ['blur', 'compose', 'copy']) this.attr(this.progs[k], 'a_xy', qb, 2);
    gl.bindVertexArray(null);
  }

  setTeeth(width, size) {
    const key = `${width}|${size}`;
    if (key === this.mouthKey) return;
    this.mouthKey = key;
    const gl = this.gl, mg = mouthGeometry(this.rig.mouth, { width, size });
    this.mouthCount = mg.length / 9;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.mouthBuf);
    gl.bufferData(gl.ARRAY_BUFFER, mg, gl.DYNAMIC_DRAW);
  }

  setTeethStyle(style) {
    const asset = teethAsset(style);
    this.teethAsset = asset;
    if (this.teethTextures.has(asset.id)) return this.teethTextures.get(asset.id).pending;
    const entry = { texture: null, pending: null };
    this.teethTextures.set(asset.id, entry);
    entry.pending = loadTeethImage(asset.url).then(image => {
      const gl = this.gl;
      if (this.disposed || gl.isContextLost()) return;
      const texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.generateMipmap(gl.TEXTURE_2D);
      entry.texture = texture;
    }).catch(e => console.warn('Photo teeth unavailable; using rendered teeth.', e));
    return entry.pending;
  }

  drawPhotoTeeth(f, texture, { blend, glowWire, glowSkin, primary }) {
    const gl = this.gl, tp = this.progs.teeth;
    const width = f.cfg.teeth_width ?? 1, size = f.cfg.teeth_size ?? 1;
    const key = `${this.teethAsset.id}|${width}|${size}`;
    if (this.teethGeometryKey !== key) {
      const vertices = teethGeometry(this.rig.mouth, this.teethAsset, width, size);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.teethBuf);
      gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.DYNAMIC_DRAW);
      this.teethCount = vertices.length / 9;
      this.teethGeometryKey = key;
    }
    gl.useProgram(tp.p);
    gl.uniformMatrix4fv(tp.u.u_proj, false, f.proj);
    gl.uniformMatrix4fv(tp.u.u_view, false, f.view);
    gl.uniformMatrix4fv(tp.u.u_model, false, f.head);
    gl.uniform1f(tp.u.u_jaw, f.jawAngle);
    gl.uniform3fv(tp.u.u_pivot, this.rig.mouth.pivot);
    gl.uniform1f(tp.u.u_zFront, this.rig.mouth.zFront);
    gl.uniform1f(tp.u.u_dim, f.dim);
    gl.uniform1f(tp.u.u_blend, blend);
    gl.uniform1f(tp.u.u_glowWire, glowWire);
    gl.uniform1f(tp.u.u_glowSkin, glowSkin);
    gl.uniform3fv(tp.u.u_primary, primary);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.uniform1i(tp.u.u_teeth, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this.vaoTeeth);
    // Lower row first; the slightly forward upper row covers it at the bite.
    const half = this.teethCount / 2;
    gl.drawArrays(gl.TRIANGLES, half, half);
    gl.drawArrays(gl.TRIANGLES, 0, half);
    gl.disable(gl.BLEND);
  }

  /** Install a rig with the supported topology and refresh every shape-dependent GPU buffer. */
  setRig(rig) {
    if (rig.count !== this.rig.count || rig.skin.length !== this.skinIdx.length ||
      rig.skin.some((v, i) => v !== this.skinIdx[i])) throw new Error('Unsupported character topology');
    const gl = this.gl, skin = this.skinIdx;
    const mask = new Float32Array(skin.length * 4), socket = new Float32Array(skin.length);
    for (let k = 0; k < skin.length; k++) {
      const v = skin[k];
      mask.set([rig.masks.edge[v], rig.masks.lips[v], rig.masks.brows[v], rig.masks.cheeks[v]], k * 4);
      socket[k] = rig.masks.socket[v];
      this.photoUV[k * 2] = rig.uvs[v * 2]; this.photoUV[k * 2 + 1] = rig.uvs[v * 2 + 1];
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.mask); gl.bufferSubData(gl.ARRAY_BUFFER, 0, mask);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.socket); gl.bufferSubData(gl.ARRAY_BUFFER, 0, socket);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.photo); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.photoUV);
    this.rig = rig;
    this.mouthKey = null;
    this.teethGeometryKey = null;
  }

  setPhoto(image, landmarks) {
    const gl = this.gl;
    if (!image) {
      if (this.photoTex) gl.deleteTexture(this.photoTex);
      this.photoTex = null;
      this.photoIris = null;
      return;
    }
    this.photoIris = irisColour(image, landmarks);
    if (!this.photoTex) this.photoTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.photoTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const skin = this.skinIdx;
    for (let k = 0; k < skin.length; k++) {
      const v = skin[k];
      this.photoUV[k * 2] = landmarks[v][0]; this.photoUV[k * 2 + 1] = landmarks[v][1];
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.photo);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.photoUV);
  }

  // offscreen targets, resized with the canvas
  targets(w, h) {
    if (this.size[0] === w && this.size[1] === h) return;
    const gl = this.gl;
    this.size = [w, h];
    const tex = (tw, th) => {
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, tw, th, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      return t;
    };
    const fbo = t => { const f = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, f); gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, t, 0); return f; };
    this.dropTargets();
    // multisampled scene: the picture, and what of it glows (the bloom's source)
    const msFbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, msFbo);
    for (const at of [gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]) {
      const rbC = gl.createRenderbuffer();
      this.msBuffers.push(rbC);
      gl.bindRenderbuffer(gl.RENDERBUFFER, rbC);
      gl.renderbufferStorageMultisample(gl.RENDERBUFFER, this.samples, gl.RGBA8, w, h);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, at, gl.RENDERBUFFER, rbC);
    }
    gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1]);
    const rbD = gl.createRenderbuffer();
    this.msBuffers.push(rbD);
    gl.bindRenderbuffer(gl.RENDERBUFFER, rbD);
    gl.renderbufferStorageMultisample(gl.RENDERBUFFER, this.samples, gl.DEPTH24_STENCIL8, w, h);
    gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_STENCIL_ATTACHMENT, gl.RENDERBUFFER, rbD);
    this.msFbo = msFbo;
    this.sceneTex = tex(w, h); this.sceneFbo = fbo(this.sceneTex);
    this.glowTex = tex(w, h); this.glowFbo = fbo(this.glowTex);
    const bw = Math.max(1, w >> 1), bh = Math.max(1, h >> 1);
    this.bloomSize = [bw, bh];
    this.bloomTexA = tex(bw, bh); this.bloomA = fbo(this.bloomTexA);
    this.bloomTexB = tex(bw, bh); this.bloomB = fbo(this.bloomTexB);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  /** Delete the offscreen targets: framebuffers, their textures and the multisampled renderbuffers. */
  dropTargets() {
    const gl = this.gl;
    for (const k of ['msFbo', 'sceneFbo', 'glowFbo', 'bloomA', 'bloomB']) if (this[k]) gl.deleteFramebuffer(this[k]);
    for (const k of ['sceneTex', 'glowTex', 'bloomTexA', 'bloomTexB']) if (this[k]) gl.deleteTexture(this[k]);
    for (const rb of this.msBuffers || []) gl.deleteRenderbuffer(rb);
    for (const k of ['msFbo', 'sceneFbo', 'glowFbo', 'bloomA', 'bloomB', 'sceneTex', 'glowTex', 'bloomTexA', 'bloomTexB']) this[k] = null;
    this.msBuffers = [];
  }

  /** Free everything this renderer made on the GPU and leave the canvas clear. The context stays: it belongs to the
   *  canvas, and another renderer can be made on it. */
  dispose() {
    const gl = this.gl;
    this.disposed = true;
    if (gl.isContextLost()) return;
    gl.useProgram(null);                    // a program in use would only be deleted once it's not
    gl.bindVertexArray(null);
    this.dropTargets();
    this.size = [0, 0];
    for (const { p } of Object.values(this.progs)) {
      for (const s of gl.getAttachedShaders(p) || []) gl.deleteShader(s);
      gl.deleteProgram(p);
    }
    const buffers = [...Object.values(this.buf), ...Object.values(this.capBuf), this.eyeBuf, this.mouthBuf, this.teethBuf, this.quadBuf];
    for (const b of buffers) gl.deleteBuffer(b);
    for (const v of [this.vaoSkin, this.vaoDepth, ...Object.values(this.vaoCap), this.vaoEye, this.vaoMouth, this.vaoTeeth, this.vaoQuad]) {
      gl.deleteVertexArray(v);
    }
    for (const { texture } of this.teethTextures.values()) if (texture) gl.deleteTexture(texture);
    this.teethTextures.clear();
    this.setPhoto(null);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
  }

  /** frame: {pose, jawAngle, head (mat4), view, proj, eyeCam, eyes: {R: mat4, L: mat4}, cfg, time, dim, talk, pupil, tongue,
   *  vanish (0..1), box ([cx, cy, half w, half h] in uv), css ([w, h]), sweep (0..1: how much of a sculpt/photo
   *  style covers the hologram, from the top down; 1 when not given), blend (0..1: there, a cross-fade from the
   *  hologram to the sculpt/photo skin; 1 when not given), spin (0..1: the eyes glow with a spinner in the pupils;
   *  0 when not given)} */
  render(f) {
    const gl = this.gl, rig = this.rig, cfg = f.cfg;
    const w = this.canvas.width, h = this.canvas.height;
    this.targets(w, h);
    // expand per-vertex data into the non-indexed skin/cap buffers
    const P = f.pose.pos, Nr = f.pose.nrm, idx = this.skinIdx;
    for (let k = 0; k < idx.length; k++) {
      const v = idx[k] * 3;
      this.skinPos[k * 3] = P[v]; this.skinPos[k * 3 + 1] = P[v + 1]; this.skinPos[k * 3 + 2] = P[v + 2];
      this.skinNrm[k * 3] = Nr[v]; this.skinNrm[k * 3 + 1] = Nr[v + 1]; this.skinNrm[k * 3 + 2] = Nr[v + 2];
    }
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.pos); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.skinPos);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.nrm); gl.bufferSubData(gl.ARRAY_BUFFER, 0, this.skinNrm);
    for (const k of ['mouth', 'eyes']) {
      const ci = this.capIdx[k], cp = this.capPos[k];
      for (let j = 0; j < ci.length; j++) { const v = ci[j] * 3; cp[j * 3] = P[v]; cp[j * 3 + 1] = P[v + 1]; cp[j * 3 + 2] = P[v + 2]; }
      gl.bindBuffer(gl.ARRAY_BUFFER, this.capBuf[k]); gl.bufferSubData(gl.ARRAY_BUFFER, 0, cp);
    }

    // a photo style whose picture hasn't arrived yet shows the wireframe until it has (not the sculpted head)
    const named = { hologram: 0, sculpt: 1, photo: 2 }[cfg.style] ?? 0;
    const style = named === 2 && !this.photoTex ? 0 : named;
    const col = k => hexRGB(cfg.colors[k]);
    // the sweep: a line moving down the face, the skin above it, the hologram below; eyes and mouth switch as it passes.
    // Where the skin is, it's a cross-fade from the hologram: blend (skin_amount) says how much of the skin shows.
    const sweep = style === 0 ? 1 : Math.min(1, Math.max(0, f.sweep ?? 1));
    const blend = style === 0 ? 0 : Math.min(1, Math.max(0, f.blend ?? 1));
    let top = -Infinity, bottom = Infinity;
    if (sweep < 1) for (let v = 1; v < P.length; v += 3) { top = Math.max(top, P[v]); bottom = Math.min(bottom, P[v]); }
    const sweepY = sweep >= 1 ? -1e4 : sweep <= 0 ? 1e4 : top + 0.5 - (top - bottom + 1) * sweep;
    const skinIn = 1 - (1 - blend) ** 2;          // the skin comes in a little ahead of the wireframe going
    const shown = y => (sweepY < y ? skinIn : 0);
    const eyeBlend = shown(rig.eyes.R.center[1]), mouthBlend = shown((P[13 * 3 + 1] + P[14 * 3 + 1]) / 2);
    // the bloom's source: the wireframe glows, the skin keeps a tenth. While a sweep runs, both glow as they always
    // have (fully at first, a tenth once the skin is a third of the way down); with less than all of the skin, the
    // wireframe's share gets its glow back as the sweep ends.
    const sweeping = style !== 0 && sweep > 0 && sweep < 1, early = 1 - 0.9 * Math.min(1, sweep * 3);
    const glowSkin = sweeping ? early : 0.1;
    const glowWire = sweeping ? 1 - (1 - early) * blend ** (sweep / (1 - sweep)) : 1;
    const glows = { glowWire, glowSkin, primary: col('primary') };

    gl.bindFramebuffer(gl.FRAMEBUFFER, this.msFbo);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clearDepth(1);
    gl.clearStencil(0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT | gl.STENCIL_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);

    const setMats = (prog, model) => {
      gl.uniformMatrix4fv(prog.u.u_proj, false, f.proj);
      gl.uniformMatrix4fv(prog.u.u_view, false, f.view);
      if (model && prog.u.u_model) gl.uniformMatrix4fv(prog.u.u_model, false, model);
    };

    // 1. skin depth
    const cap = this.progs.cap;
    gl.useProgram(cap.p);
    setMats(cap, f.head);
    gl.colorMask(false, false, false, false);
    gl.depthMask(true);
    gl.depthFunc(gl.LESS);
    gl.enable(gl.POLYGON_OFFSET_FILL);
    gl.polygonOffset(1, 2);                          // the colour pass must win against its own depth
    gl.bindVertexArray(this.vaoDepth);
    gl.drawArrays(gl.TRIANGLES, 0, idx.length);
    gl.disable(gl.POLYGON_OFFSET_FILL);
    // 2. openings -> stencil (mouth = 1, eyes = 2), only where visible
    gl.depthMask(false);
    gl.depthFunc(gl.LEQUAL);
    gl.enable(gl.STENCIL_TEST);
    gl.stencilOp(gl.KEEP, gl.KEEP, gl.REPLACE);
    gl.stencilFunc(gl.ALWAYS, 1, 0xff);
    gl.bindVertexArray(this.vaoCap.mouth);
    gl.drawArrays(gl.TRIANGLES, 0, this.capIdx.mouth.length);
    if (cfg.eyes) {
      gl.stencilFunc(gl.ALWAYS, 2, 0xff);
      gl.bindVertexArray(this.vaoCap.eyes);
      gl.drawArrays(gl.TRIANGLES, 0, this.capIdx.eyes.length);
    }
    gl.colorMask(true, true, true, true);
    gl.stencilOp(gl.KEEP, gl.KEEP, gl.KEEP);
    // 3. interiors, clipped to their openings (the openings hold no skin depth, so they pass)
    gl.depthMask(true);
    gl.depthFunc(gl.LESS);
    gl.stencilFunc(gl.EQUAL, 1, 0xff);
    const mp = this.progs.mouth;
    this.setTeeth(cfg.teeth_width ?? 1, cfg.teeth_size ?? 1);
    const photoMouth = style === 2 && mouthBlend > 0;
    const photoTeeth = photoMouth && this.teethTextures.get(this.teethAsset.id)?.texture;
    // The canonical rig retains a hairline seam even at full closure. Keep it dark,
    // rather than allowing enamel to flicker through that sub-pixel opening.
    const lipsSealed = photoMouth && P[13 * 3 + 1] - P[14 * 3 + 1] < rig.mouth.halfW * 0.02;
    gl.useProgram(mp.p);
    setMats(mp, f.head);
    gl.uniform1f(mp.u.u_jaw, f.jawAngle);
    gl.uniform3fv(mp.u.u_pivot, rig.mouth.pivot);
    gl.uniform2fv(mp.u.u_tongue, f.tongue || [0, 0]);
    gl.uniform3fv(mp.u.u_primary, col('primary'));
    gl.uniform3fv(mp.u.u_mouth, col('mouth'));
    gl.uniform1f(mp.u.u_zFront, rig.mouth.zFront);
    gl.uniform1f(mp.u.u_dim, f.dim);
    gl.uniform1f(mp.u.u_blend, mouthBlend);
    gl.uniform1f(mp.u.u_glowWire, glowWire);
    gl.uniform1f(mp.u.u_glowSkin, glowSkin);
    gl.uniform1i(mp.u.u_hideDentition, photoTeeth || lipsSealed ? 1 : 0);
    gl.bindVertexArray(this.vaoMouth);
    gl.drawArrays(gl.TRIANGLES, 0, this.mouthCount);
    if (photoTeeth && !lipsSealed) this.drawPhotoTeeth(f, photoTeeth, { blend: mouthBlend, ...glows });
    // eyeballs
    if (cfg.eyes) {
      gl.stencilFunc(gl.EQUAL, 2, 0xff);
      const ep = this.progs.eye;
      gl.useProgram(ep.p);
      gl.uniformMatrix4fv(ep.u.u_proj, false, f.proj);
      gl.uniformMatrix4fv(ep.u.u_view, false, f.view);
      const ownIris = style === 2 && this.photoIris && cfg.iris_from_photo !== false;
      gl.uniform3fv(ep.u.u_iris, ownIris ? this.photoIris : col('iris'));
      gl.uniform3fv(ep.u.u_primary, col('primary'));
      gl.uniform3fv(ep.u.u_eye, f.eyeCam);
      gl.uniform1f(ep.u.u_pupil, f.pupil);
      gl.uniform1f(ep.u.u_dim, f.dim);
      gl.uniform1f(ep.u.u_blend, eyeBlend);
      gl.uniform1f(ep.u.u_glowWire, glowWire);
      gl.uniform1f(ep.u.u_glowSkin, glowSkin);
      gl.uniform1f(ep.u.u_spin, f.spin || 0);
      gl.uniform1f(ep.u.u_time, f.time);
      gl.bindVertexArray(this.vaoEye);
      for (const s of ['R', 'L']) {
        gl.uniformMatrix4fv(ep.u.u_eyeModel, false, f.eyes[s]);
        gl.drawArrays(gl.TRIANGLES, 0, this.eyeCount);
      }
    }
    gl.disable(gl.STENCIL_TEST);
    // 4. skin colour
    gl.depthMask(false);
    gl.depthFunc(gl.LEQUAL);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    const sp = this.progs.skin;
    gl.useProgram(sp.p);
    setMats(sp, f.head);
    gl.uniform1i(sp.u.u_style, style);
    gl.uniform1f(sp.u.u_sweepY, sweepY);
    gl.uniform1f(sp.u.u_sweeping, sweeping ? Math.min(1, blend * 4) : 0);   // no band when no skin is coming
    gl.uniform1f(sp.u.u_blend, blend);
    gl.uniform1f(sp.u.u_skinIn, skinIn);
    gl.uniform1f(sp.u.u_glowWire, glowWire);
    gl.uniform1f(sp.u.u_glowSkin, glowSkin);
    for (const [u, k] of [['u_primary', 'primary'], ['u_secondary', 'secondary'], ['u_skin', 'skin'], ['u_lips', 'lips']]) gl.uniform3fv(sp.u[u], col(k));
    gl.uniform3fv(sp.u.u_eye, f.eyeCam);
    gl.uniform1f(sp.u.u_wire, cfg.wire);
    gl.uniform1f(sp.u.u_fill, cfg.fill);
    gl.uniform1f(sp.u.u_scan, cfg.scanlines);
    gl.uniform1f(sp.u.u_points, cfg.points);
    gl.uniform1f(sp.u.u_edgeFade, cfg.edge_fade);
    gl.uniform1f(sp.u.u_time, f.time);
    gl.uniform1f(sp.u.u_glow, cfg.glow);
    gl.uniform1f(sp.u.u_dim, f.dim);
    gl.uniform1f(sp.u.u_talk, f.talk || 0);
    if (this.photoTex) {
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.photoTex);
      gl.uniform1i(sp.u.u_photoTex, 0);
    }
    gl.bindVertexArray(this.vaoSkin);
    gl.drawArrays(gl.TRIANGLES, 0, idx.length);
    gl.disable(gl.BLEND);
    gl.depthMask(true);
    gl.disable(gl.DEPTH_TEST);

    // resolve MSAA: the picture, and what of it glows
    const glow = cfg.glow * f.dim;
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, this.msFbo);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, this.sceneFbo);
    gl.blitFramebuffer(0, 0, w, h, 0, 0, w, h, gl.COLOR_BUFFER_BIT, gl.NEAREST);
    if (glow > 0.01) {
      gl.readBuffer(gl.COLOR_ATTACHMENT1);
      gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, this.glowFbo);
      gl.blitFramebuffer(0, 0, w, h, 0, 0, w, h, gl.COLOR_BUFFER_BIT, gl.NEAREST);
      gl.readBuffer(gl.COLOR_ATTACHMENT0);
    }

    // 5. bloom + compose
    gl.bindVertexArray(this.vaoQuad);
    if (glow > 0.01) {
      const [bw, bh] = this.bloomSize;
      const blur = this.progs.blur;
      gl.useProgram(blur.p);
      gl.viewport(0, 0, bw, bh);
      gl.activeTexture(gl.TEXTURE0);
      const pass = (src, dst, dx, dy) => {
        gl.bindFramebuffer(gl.FRAMEBUFFER, dst);
        gl.bindTexture(gl.TEXTURE_2D, src);
        gl.uniform1i(blur.u.u_tex, 0);
        gl.uniform2f(blur.u.u_dir, dx, dy);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      };
      pass(this.glowTex, this.bloomA, 1 / w, 0);
      for (let i = 0; i < 3; i++) {
        const r = 1 + i * 1.5;
        pass(this.bloomTexA, this.bloomB, 0, r / bh);
        pass(this.bloomTexB, this.bloomA, r / bw, 0);
      }
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const cp = this.progs.compose;
    gl.useProgram(cp.p);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.sceneTex);
    gl.uniform1i(cp.u.u_scene, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.bloomTexA);
    gl.uniform1i(cp.u.u_bloom, 1);
    gl.uniform1f(cp.u.u_glow, glow > 0.01 ? glow : 0);
    gl.uniform1f(cp.u.u_vanish, f.vanish || 0);
    gl.uniform1f(cp.u.u_time, f.time);
    gl.uniform4fv(cp.u.u_box, f.box || [0.5, 0.5, 0.35, 0.4]);
    gl.uniform2fv(cp.u.u_res, f.css || [w, h]);
    gl.uniform3fv(cp.u.u_tint, col('primary'));
    gl.drawArrays(gl.TRIANGLES, 0, 6);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindVertexArray(null);
  }
}


// Eye corners in the 468-point mesh (MediaPipe): right eye 33-133, left eye 362-263.
const EYES = [[33, 133], [362, 263]];

/** The iris colour in a photo: the middle band of the darker pixels around each eye's centre (darker than skin and
 *  sclera, lighter than pupil and lashes), as linear-ish RGB 0..1; null if the photo can't be read. */
export function irisColour(image, landmarks) {
  try {
    const w = image.width, h = image.height;
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(image, 0, 0);
    const px = [];
    for (const [a, b] of EYES) {
      const [ax, ay] = landmarks[a], [bx, by] = landmarks[b];
      const cx = (ax + bx) / 2 * w, cy = (ay + by) / 2 * h, r = Math.max(2, Math.hypot((bx - ax) * w, (by - ay) * h) * 0.16);
      const d = g.getImageData(Math.round(cx - r), Math.round(cy - r), Math.round(2 * r), Math.round(2 * r)).data;
      for (let i = 0; i < d.length; i += 4) px.push([d[i], d[i + 1], d[i + 2], 0.3 * d[i] + 0.59 * d[i + 1] + 0.11 * d[i + 2]]);
    }
    if (px.length < 20) return null;
    px.sort((p, q) => p[3] - q[3]);
    const band = px.slice(Math.floor(px.length * 0.12), Math.floor(px.length * 0.45));
    const avg = [0, 1, 2].map(k => band.reduce((s, p) => s + p[k], 0) / band.length / 255);
    const lift = 1.35;                                           // the shader darkens the iris towards its rim
    return avg.map(v => Math.min(1, v * lift));
  } catch {
    return null;
  }
}
