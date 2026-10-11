import { blendRigs } from './rig-blend.js';
import { canonicalRig } from './canonical.js';
/*! Wireface | Required Notice: Copyright 2026 Compsmart (https://wireface.dev) | Wireface SDK Licence; free local use including commercial: https://wireface.dev/license/ */
// FaceView: one animated face on a canvas. Used by the desktop face window (face.html) and the
// browser page alike. Feed it the backend's WebSocket messages with ``message()``.
import { Pose } from './rig.js';
import { Renderer, M4 } from './render.js';
import { Animator, moodFor } from './animator.js';
import { MouthTimeline } from './timeline.js';

export const DEFAULT_CONFIG = {
  mode: 'floating', style: 'hologram',
  colors: { primary: '#d97757', secondary: '#f2c38f', skin: '#c9a48e', iris: '#4f86c6', lips: '#b06a6e', mouth: '#5c1018', background: '#14100c' },
  glow: 1, wire: 0.85, fill: 0.35, scanlines: 0.3, points: 0, edge_fade: 0.5, background: 'gradient', eyes: true,
  iris_from_photo: true, teeth_width: 1, teeth_size: 1, teeth_style: 'normal',
  follow_mouse: true, motion: 1, expressiveness: 1, mouth_gain: 1, sync_offset_ms: 0, captions: true, on_top: true,
  zoom: 1, photo: null,
};

const FOV = 24 * Math.PI / 180;
// A frame drawn now reaches the screen ~2 refreshes later (compositor + scan-out): pose the mouth for then.
// Measured 2026-10-01: without this the drawn mouth trailed its schedule by ~60 ms and the voice visibly led.
const DISPLAY_LATENCY_S = 0.035;
export const FACE_CENTER_Y = -0.55;
const NECK = [0, -4.5, -3.2];

/** Map the app's status (+ recent speech activity) to the face's state. It sleeps while nothing is there to talk to:
 *  no connection to the face's own server, or the agent offline or stopped (the beacon's offline / error), unless it
 *  is speaking. */
export function faceState(status, { speaking, userSpeakingAt, now }) {
  const s = status?.state;
  if (!s || s === 'stopped' || s === 'gpu_free') return 'asleep';
  const agent = status.beacon?.state;
  if (!speaking && s !== 'speaking' && (agent === 'offline' || agent === 'error')) return 'asleep';
  if (s === 'starting' || s === 'busy' || agent === 'connecting') return 'busy';
  if (s === 'error') return 'error';
  if (speaking || s === 'speaking') return 'speaking';
  if (s === 'thinking') return 'thinking';
  if (now - userSpeakingAt < 1.5) return 'listening';
  if (s === 'muted') return 'muted';
  return 'idle';
}

export class FaceView {
  constructor(canvas, { api, zoomKey = 'zoom', fit = 1 } = {}) {
    this.canvas = canvas;
    this.api = api;                     // (path) => Promise<Response>, adds the token
    this.cfg = structuredClone(DEFAULT_CONFIG);
    this.rig = canonicalRig();
    this.pose = new Pose(this.rig);
    this.renderer = new Renderer(canvas, this.rig);
    this.animator = new Animator();
    this.timeline = new MouthTimeline();
    this.status = null;
    this.speaking = false;
    this.userSpeakingAt = -1e9;
    this.pointer = null;                // [x, y] css px from the face centre (y down), or null
    this.pointerSeen = 0;
    this.zoomKey = zoomKey;             // 'zoom' applies cfg.zoom; null = fixed framing (floating window)
    this.fit = fit;
    this.centerY = FACE_CENTER_Y;       // world y the framing centres on
    this.photoVersion = null;
    this.hull = [];
    this.hullAt = 0;
    this.lastT = performance.now() / 1000;
    this.raf = 0;
    this.listeners = new Set();
    this.lost = false;
    this.vanish = 0;                    // 0 = on screen, 1 = gone ("go away"); in between, the transition
    this.trans = null;
    this.sweepAt = 1;                   // how much of a sculpt/photo style covers the hologram (sweep())
    this.sweepTr = null;
    this.blend = null;                  // skin_amount as shown: it glides to a new value
    this.spinning = false;              // waiting: the eyes glow, with a spinner in the pupils (setSpinner)
    this.spin = 0;                      // how much of that shows: it fades in and out
    this.photoLoading = Promise.resolve();   // settles once the photo asked for by the last setConfig is on
    this.vanishBox = null;
    this.drawnGone = false;
    this.destroyed = false;
    // a GPU reset (driver update, a game grabbing the GPU) loses the context: rebuild on restore
    this.canvasEvents = {
      webglcontextlost: e => { e.preventDefault(); this.lost = true; },
      webglcontextrestored: () => {
        this.renderer = new Renderer(canvas, this.rig);
        this.lost = false;
        this.renderer.setTeethStyle(this.cfg.teeth_style);
        const v = this.photoVersion;
        if (this.installedCharacter) this.renderer.setPhoto(this.installedCharacter.image, this.installedCharacter.landmarks);
        else if (v) this.loadPhoto(v).catch(err => console.warn('face photo', err));
      },
    };
    for (const [type, fn] of Object.entries(this.canvasEvents)) canvas.addEventListener(type, fn);
  }

  /** Supported rig replacement; interrupted transitions start from the currently displayed shape. */
  installRig(rig, { transition = 0, now = performance.now() / 1000 } = {}) {
    if (!Number.isFinite(transition) || transition < 0) throw new TypeError('Invalid transition duration');
    const from = this.rigTransition ? blendRigs(this.rig, this.rig, 0) : this.rig;
    if (transition > 0) {
      const working = blendRigs(from, rig, 0);
      this.rigTransition = { from, to: rig, working, start: now, duration: transition };
      this._installRig(working);
    } else { this.rigTransition = null; this._installRig(rig); }
    return rig;
  }

  _installRig(rig) {
    if (!this.lost) this.renderer.setRig(rig);
    this.rig = rig;
    if (this.pose) this.pose.rig = rig;
    else this.pose = new Pose(rig);
  }

  stepRig(now = performance.now() / 1000) {
    const t = this.rigTransition;
    if (!t) return;
    const progress = Math.max(0, Math.min(1, (now - t.start) / t.duration));
    if (progress === 1) { this.rigTransition = null; this._installRig(t.to); }
    else this._installRig(blendRigs(t.from, t.to, progress * progress * (3 - 2 * progress), t.working));
  }

  installCharacter(character, image, options) {
    this.installRig(character.rig, options);
    const landmarks = Array.from({ length: 468 }, (_, i) => [character.rig.uvs[i * 2], character.rig.uvs[i * 2 + 1]]);
    if (!this.lost) this.renderer.setPhoto(image, landmarks);
    const previous = this.installedCharacter;
    this.installedCharacter = { character, image, landmarks };
    if (previous?.image !== image) previous?.image?.close();
    this.photoVersion = null;
    this.cfg.photo = null;
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit(ev) { for (const f of this.listeners) f(ev); }

  setConfig(cfg) {
    this.cfg = { ...DEFAULT_CONFIG, ...cfg, colors: { ...DEFAULT_CONFIG.colors, ...(cfg?.colors || {}) } };
    if (!this.lost) this.renderer.setTeethStyle(this.cfg.teeth_style);
    const v = this.cfg.photo?.version || null;
    if (v !== this.photoVersion) {
      this.photoVersion = v;
      if (v) this.photoLoading = this.loadPhoto(v).catch(e => console.warn('face photo', e));
      else this.renderer.setPhoto(null);
    }
  }

  /** The api answers each with a Response; the photo may instead be the picture itself (an ImageBitmap, which stays
   *  its owner's to close). */
  async loadPhoto(version) {
    if (!this.api) return;
    const get = async what => {
      const r = await this.api(`/api/face/${what}?v=${version}`);
      if (r instanceof Response && !r.ok) throw new Error(`face ${what}: HTTP ${r.status}`);
      return r;
    };
    const [photo, lm] = await Promise.all([get('photo'), get('landmarks').then(r => r.json())]);
    const img = photo instanceof Response ? await createImageBitmap(await photo.blob()) : photo;
    try {
      if (version === this.photoVersion && !this.destroyed) this.renderer.setPhoto(img, lm);
    } finally {
      if (img !== photo) img.close();     // a copy decoded here isn't needed once it's on the GPU
    }
  }

  /** a backend WebSocket message */
  message(m) {
    switch (m.type) {
      case 'mouth': this.timeline.add(m); break;
      case 'mouth_clock': this.timeline.setClock(m); break;
      case 'mouth_flush': this.timeline.flush(); break;
      case 'status': this.status = m; this.applyMood(m.mood); break;
      case 'face_config': this.setConfig(m.config); break;
      case 'event': this.event(m.ev); break;
      default: break;
    }
  }

  event(ev) {
    const now = performance.now() / 1000;
    if (ev.topic === 'percept.audio.partial') this.userSpeakingAt = now;
    else if (ev.topic === 'actor.speaking') {
      this.speaking = ev.payload?.state === 'start';
      if (this.speaking) {
        const text = ev.payload?.text || '';
        this.animator.setMood(moodFor(text), now + 1.2 + text.length * 0.065);
        this.emit({ type: 'speaking', text });
      } else {
        this.caption = null;              // so the same line said again is captioned again
        this.emit({ type: 'speaking', text: null });
      }
    } else if (ev.topic === 'mood.state') {
      this.applyMood(ev.payload);
    } else if (ev.topic === 'face.expression') {
      this.animator.express(ev.payload?.name, now + (ev.payload?.seconds ?? 4));
    } else if (ev.topic === 'control.halt') {
      this.speaking = false;
      this.caption = null;
      this.timeline.flush();
      this.emit({ type: 'speaking', text: null });
    }
  }

  /** her standing mood (a mood.state snapshot; face is null when the mood is off or not shown on the face) */
  applyMood(snap) {
    this.animator.setBase(snap?.face || null, snap?.intensity ?? 0.5);
  }

  state() {
    return faceState(this.status, { speaking: this.speaking, userSpeakingAt: this.userSpeakingAt, now: performance.now() / 1000 });
  }

  setPointer(x, y) { this.pointer = x == null ? null : [x, y]; this.pointerSeen = performance.now(); }

  /** Something to wait on: the eyes glow, with a spinner turning in the wide pupils (on), or not (off). */
  setSpinner(on) { this.spinning = !!on; }

  /** Go away: a glitch, then the face burns away from the chin up in a shower of sparks. */
  disappear() { return this.transition(1, 1.5); }
  /** Come back: the same, backwards (it materialises from the top down). */
  appear() { return this.transition(0, 1.3); }
  /** Gone at once, without the animation (a page reloaded while the face is away). */
  setGone() { this.trans?.done(); this.trans = null; this.vanish = 1; }

  /** Animate from wherever it is now; resolves when it gets there, or when it is turned around. */
  transition(to, seconds) {
    this.trans?.done();
    this.trans = null;
    if (this.vanish === to) return Promise.resolve();
    const b = this.bounds(), [w, h] = this.cssSize || [0, 0];
    if (b && w && h) this.vanishBox = [b.cx / w, 1 - b.cy / h, b.w / 2 / w, (b.bottom - b.top) / 2 / h];
    this.drawnGone = false;
    this.animator.express('happy', performance.now() / 1000 + seconds + 0.3);
    return new Promise(resolve => {
      const tr = { from: this.vanish, to, seconds: seconds * Math.abs(to - this.vanish), start: null };
      // frames stop in a hidden window: don't wait for them forever
      const timer = setTimeout(() => { if (this.trans === tr) { this.vanish = to; this.trans = null; } tr.done(); }, seconds * 1000 + 600);
      tr.done = () => { clearTimeout(timer); resolve(); };
      this.trans = tr;
    });
  }

  /** Sweep the configured sculpt/photo style onto the hologram wireframe from the top down (to = 1), or back off it
   *  (to = 0), over ``seconds``; resolves when done. A hologram style ignores it. */
  sweep(to, seconds = 1.6) {
    this.sweepTr?.done();
    this.sweepTr = null;
    if (this.sweepAt === to) return Promise.resolve();
    return new Promise(resolve => {
      const tr = { from: this.sweepAt, to, seconds: seconds * Math.abs(to - this.sweepAt), start: null };
      const timer = setTimeout(() => { if (this.sweepTr === tr) { this.sweepAt = to; this.sweepTr = null; } tr.done(); }, seconds * 1000 + 600);
      tr.done = () => { clearTimeout(timer); resolve(); };
      this.sweepTr = tr;
    });
  }

  stepSweep(t) {
    const tr = this.sweepTr;
    if (!tr) return;
    if (tr.start === null) tr.start = t;
    const k = tr.seconds > 0 ? Math.min(1, (t - tr.start) / tr.seconds) : 1;
    this.sweepAt = tr.from + (tr.to - tr.from) * (k < 0.5 ? 2 * k * k : 1 - 2 * (1 - k) * (1 - k));   // ease in-out
    if (k >= 1) { this.sweepTr = null; tr.done(); }
  }

  stepVanish(t) {
    const tr = this.trans;
    if (!tr) return;
    if (tr.start === null) tr.start = t;           // from the first frame drawn (the window may only just be showing)
    const k = tr.seconds > 0 ? Math.min(1, (t - tr.start) / tr.seconds) : 1;
    this.vanish = tr.from + (tr.to - tr.from) * k;
    if (k >= 1) { this.trans = null; tr.done(); }
  }

  start() {
    if (this.raf || this.destroyed) return;
    const loop = () => { this.raf = requestAnimationFrame(loop); this.frame(); };
    this.raf = requestAnimationFrame(loop);
  }

  stop() { cancelAnimationFrame(this.raf); this.raf = 0; }

  resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const r = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
    return [r.width, r.height];
  }

  matrices(cssW, cssH, a) {
    const aspect = cssW / Math.max(1, cssH);
    const half = Math.tan(FOV / 2);
    const zoom = (this.zoomKey ? this.cfg[this.zoomKey] || 1 : 1) * this.fit;
    const dist = Math.max(10.4 / half, 8.8 / (half * aspect)) / zoom;
    const proj = M4.persp(FOV, aspect, 1, dist + 60);
    const view = M4.translate(0, -this.centerY, -dist);
    const h = a.head;
    let head = M4.translate(...NECK);
    head = M4.mul(head, M4.rotY(h.yaw));
    head = M4.mul(head, M4.rotX(-h.pitch));
    head = M4.mul(head, M4.rotZ(h.roll));
    head = M4.mul(head, M4.translate(-NECK[0], -NECK[1] + h.y, -NECK[2]));
    const eyes = {};
    for (const s of ['R', 'L']) {
      const e = this.rig.eyes[s];
      let m = M4.mul(head, M4.translate(...e.center));
      // converge slightly towards a point ~60 cm in front
      const conv = (s === 'R' ? 1 : -1) * Math.atan(Math.abs(e.center[0]) / 60);
      m = M4.mul(m, M4.rotY(a.gaze[0] + conv));
      m = M4.mul(m, M4.rotX(-a.gaze[1]));
      eyes[s] = M4.mul(m, M4.scale(e.radius));
    }
    return { proj, view, head, eyes, eyeCam: [0, this.centerY, dist], dist };
  }

  pointerAngles() {
    if (!this.pointer || performance.now() - this.pointerSeen > 4000) return null;
    const [x, y] = this.pointer;
    return [Math.atan2(x, 700), Math.atan2(-y, 700)];
  }

  frame() {
    this.stepRig();
    if (this.lost) return;
    const t = performance.now() / 1000;
    const dt = Math.max(0.0005, Math.min(0.1, t - this.lastT));
    this.lastT = t;
    this.stepVanish(t);
    this.stepSweep(t);
    const spun = this.spin + ((this.spinning ? 1 : 0) - this.spin) * (1 - Math.exp(-dt * 5));
    this.spin = !this.spinning && spun < 0.002 ? 0 : spun;
    if (this.vanish >= 1 && this.drawnGone) return;         // away: one empty frame is enough
    const [cssW, cssH] = this.resize();
    const wall = Date.now() / 1000 + DISPLAY_LATENCY_S - (this.cfg.sync_offset_ms || 0) / 1000;
    const mouth = this.timeline.sample(wall);
    if (mouth?.text && mouth.text !== this.caption) {        // the sentence now reaching the speaker
      this.caption = mouth.text;
      this.emit({ type: 'caption', text: mouth.text });
    }
    const a = this.animator.update(t, dt, { state: this.state(), mouth, pointer: this.pointerAngles(), cfg: this.cfg });
    if (this.spin) a.gaze = a.gaze.map(g => g * (1 - 0.85 * this.spin));     // the spinner is shown to the viewer
    this.pose.apply(a.weights, a.jaw);
    const mats = this.matrices(cssW, cssH, a);
    const target = this.cfg.skin_amount ?? 1;                   // wireframe (0) to skin (1): a cross-fade that glides
    const eased = this.blend + (target - this.blend) * (1 - Math.exp(-dt * 4));
    this.blend = this.blend === null || Math.abs(target - eased) < 0.001 ? target : eased;
    this.renderer.render({
      pose: this.pose, jawAngle: this.pose.jawAngle, ...mats, cfg: this.cfg, time: t, dim: a.dim,
      talk: a.talk, pupil: a.pupil + (0.36 - a.pupil) * this.spin,          // wide pupils make room for the spinner
      tongue: a.tongue, vanish: this.vanish, box: this.vanishBox, css: [cssW, cssH],
      sweep: this.sweepAt, blend: this.blend, spin: this.spin,
    });
    this.drawnGone = this.vanish >= 1;
    this.lastMats = mats;
    this.cssSize = [cssW, cssH];
    if (t - this.hullAt > 0.12) { this.hullAt = t; this.updateHull(); }
  }

  /** convex hull of the projected face, in css px relative to the canvas */
  updateHull() {
    const m = this.lastMats;
    if (!m) return;
    const mvp = M4.mul(m.proj, M4.mul(m.view, m.head));
    const [w, h] = this.cssSize;
    const P = this.pose.pos, pts = [];
    for (let i = 0; i < P.length; i += 9) {          // every third vertex is plenty for a hull
      const q = M4.apply(mvp, [P[i], P[i + 1], P[i + 2]]);
      pts.push([(q[0] * 0.5 + 0.5) * w, (0.5 - q[1] * 0.5) * h]);
    }
    pts.sort((p, q) => p[0] - q[0] || p[1] - q[1]);
    const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const lower = [], upper = [];
    for (const p of pts) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
    for (let i = pts.length - 1; i >= 0; i--) { const p = pts[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
    this.hull = lower.slice(0, -1).concat(upper.slice(0, -1));
  }

  /** is css point (x, y) on the face (with a small margin)? */
  hitTest(x, y, margin = 8) {
    if (this.vanish > 0.5) return false;
    const H = this.hull;
    if (H.length < 3) return false;
    let cx = 0, cy = 0;
    for (const p of H) { cx += p[0]; cy += p[1]; }
    cx /= H.length; cy /= H.length;
    for (let i = 0; i < H.length; i++) {
      const a = H[i], b = H[(i + 1) % H.length];
      const ex = b[0] - a[0], ey = b[1] - a[1], len = Math.hypot(ex, ey) || 1;
      const side = (ex * (y - a[1]) - ey * (x - a[0])) / len;
      const inside = (ex * (cy - a[1]) - ey * (cx - a[0])) > 0;
      if (inside ? side < -margin : side > margin) return false;
    }
    return true;
  }

  /** where the face is, for placing UI: {cx, cy, top, bottom, w} in css px */
  bounds() {
    const H = this.hull;
    if (!H.length) return null;
    let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
    for (const [x, y] of H) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
    return { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, top: y0, bottom: y1, w: x1 - x0 };
  }

  /** Stop, and free what it holds on the GPU. The context itself stays with the canvas, so another face can use it. */
  destroy() {
    this.stop();
    this.destroyed = true;
    this.installedCharacter?.image?.close();
    this.installedCharacter = null;
    this.listeners.clear();
    for (const [type, fn] of Object.entries(this.canvasEvents)) this.canvas.removeEventListener(type, fn);
    this.sweepTr?.done();
    this.trans?.done();
    this.renderer.dispose();
  }
}
