/*! Wireface | Required Notice: Copyright 2026 Compsmart (https://wireface.dev) | Wireface SDK Licence; free local use including commercial: https://wireface.dev/license/ */
// Animation layers for the face: expression (state + mood), lip sync, blinks, gaze, head motion.
// Two kinds of mood: a short cue from each sentence she says (moodFor), and her standing mood from the
// mood service (setBase), which she wears whenever nothing else is showing and which sets her body language.
// Everything is driven by wall-clock time, so the result doesn't depend on the frame rate.
import { CH, C } from './rig.js';

export const EXPRESSIONS = {
  neutral: {},
  happy: { smileL: 0.7, smileR: 0.7, squintL: 0.22, squintR: 0.22, browUpL: 0.1, browUpR: 0.1 },
  amused: { smileL: 0.45, smileR: 0.7497, squintR: 0.3, browUpL: 0.22 },
  sad: { frown: 0.5503, browInner: 0.8469, blinkL: 0.16, blinkR: 0.16, pout: 0.25 },
  concerned: { browInner: 0.5495, browDown: 0.18, frown: 0.22 },
  surprised: { browUpL: 0.85, browUpR: 0.85, eyeWideL: 0.75, eyeWideR: 0.75, jaw: 0.22038 },
  afraid: { browInner: 0.9, browUpL: 0.45, browUpR: 0.45, eyeWideL: 0.9, eyeWideR: 0.9, wide: 0.39833, frown: 0.25, jaw: 0.1 },
  thinking: { browDown: 0.27955, browUpL: 0.3, pout: 0.21948, close: 0.25, smileR: 0.08 },
  listening: { browUpL: 0.14, browUpR: 0.14, eyeWideL: 0.1, eyeWideR: 0.1, smileL: 0.06, smileR: 0.06 },
  // asleep: lids closed and heavy, brows and cheeks let go, lips just parted
  asleep: { blinkL: 1, blinkR: 1, browInner: 0.06, browDown: 0.08, squintL: 0.18, squintR: 0.18, pout: 0.1, jaw: 0.05 },
  muted: { blinkL: 0.25, blinkR: 0.25 },
  error: { browInner: 0.6989, browDown: 0.25, frown: 0.3 },
  busy: { browDown: 0.15, squintL: 0.15, squintR: 0.15 },
  // standing moods (mind/mood.py): happy and sad above double as two of them
  excited: { smileL: 0.8, smileR: 0.8, eyeWideL: 0.35, eyeWideR: 0.35, browUpL: 0.4, browUpR: 0.4, jaw: 0.07964 },
  content: { smileL: 0.35, smileR: 0.35, blinkL: 0.2, blinkR: 0.2, squintL: 0.12, squintR: 0.12 },
  curious: { browUpL: 0.5479, browUpR: 0.12046, eyeWideL: 0.25, eyeWideR: 0.25, smileR: 0.12 },
  calm: { smileL: 0.08, smileR: 0.08 },
  bored: { blinkL: 0.38, blinkR: 0.38, pout: 0.2, browInner: 0.08, close: 0.14948 },
  frustrated: { browDown: 0.6015, squintL: 0.35, squintR: 0.35, close: 0.4, frown: 0.25, sneer: 0.1 },
  glum: { frown: 0.29913, browInner: 0.4, blinkL: 0.2, blinkR: 0.2 },
};

// body language per standing mood: energy scales gestures and head motion, droop lowers the head, tilt rolls it
const MOOD_MOTION = {
  excited: { energy: 1.5 }, happy: { energy: 1.2 }, content: { energy: 0.8 }, curious: { energy: 1.2454, tilt: 0.06 },
  calm: {}, bored: { energy: 0.55, droop: 0.05 }, frustrated: { energy: 1.1 }, glum: { energy: 0.7, droop: 0.04 },
  sad: { energy: 0.5, droop: 0.08 },
};

const STATE_EXPR = { asleep: 'asleep', thinking: 'thinking', listening: 'listening', muted: 'muted', error: 'error', busy: 'busy' };

/** A light mood cue from what's being said (kept subtle; it fades back after the sentence). */
export function moodFor(text) {
  const t = ` ${(text || '').toLowerCase()} `;
  if (/\b(sorry|unfortunately|afraid|can't|cannot|couldn't|unable|failed|bad news|apologi[sz]e)\b/.test(t)) return 'concerned';
  if (/\b(wow|whoa|amazing|incredible|no way|really\?)/.test(t)) return 'surprised';
  if (/\b(haha|ha ha|lol|funny|joke)\b/.test(t)) return 'amused';
  if (/\b(great|glad|happy|awesome|nice|love|fantastic|wonderful|congrat\w*|excellent|perfect|you're welcome|good news|hello|hi|hey|done)\b|!/.test(t)) return 'happy';
  if (/\b(hmm|let me think|not sure|maybe|perhaps|i think|might)\b/.test(t)) return 'thinking';
  return 'neutral';
}

// smooth pseudo-noise from incommensurate sines
const noise = (t, s) => Math.sin(t * 0.31 + s * 1.7) * 0.55 + Math.sin(t * 0.11263 + s * 3.1) * 0.3 + Math.sin(t * 0.7267 + s * 5.3) * 0.15;
const approach = (cur, target, rate, dt) => cur + (target - cur) * (1 - Math.exp(-rate * dt));

const GESTURES = [
  { dur: 0.7, ch: { browUpL: 0.35, browUpR: 0.28043 }, talkSafe: true },        // brow flash
  { dur: 1.3, ch: { smileR: 0.3 }, talkSafe: false },                         // half smile
  { dur: 1.3, ch: { smileL: 0.3 }, talkSafe: false },
  { dur: 1.2, ch: { squintL: 0.25, squintR: 0.25 }, talkSafe: true },
  { dur: 1.4, ch: { browDown: 0.25 }, talkSafe: true },
  { dur: 1.1, ch: { close: 0.35, pout: 0.15 }, talkSafe: false },
];

export class Animator {
  constructor() {
    this.w = new Float32Array(CH.length);
    this.expr = new Float32Array(CH.length);
    this.exprJaw = 0;
    this.mouth = { jaw: 0, wide: 0, round: 0, close: 0, fric: 0, bite: 0, tongue: 0, energy: 0 };
    this.mood = 'neutral';
    this.moodUntil = 0;
    this.shown = null;           // an expression asked for (the agent's face tool, the panel): wins until shownUntil
    this.shownUntil = 0;
    this.base = null;            // standing mood (null: mood off)
    this.baseStrength = 1;
    this.energy = 1;
    this.droop = 0;
    this.tilt = 0;
    this.nextBlink = 1.2;
    this.blinkT = -1;
    this.doubleBlink = false;
    this.nextSaccade = 0.6;
    this.saccade = [0, 0];
    this.gaze = [0, 0];
    this.head = { yaw: 0, pitch: 0, roll: 0, y: 0 };
    this.emph = 0;
    this.thinkSide = 1;
    this.lastState = '';
    this.gestures = [];
    this.nextGesture = 3;
    this.pupil = 0.2;
    this.dim = 1;
    this.sleep = 0;              // 0 awake .. 1 fast asleep: eases in, so falling asleep and waking take a moment
  }

  setMood(name, seconds) {
    this.mood = EXPRESSIONS[name] ? name : 'neutral';
    this.moodUntil = seconds;
  }

  /** Show an expression until ``until`` (seconds, the update clock), over the automatic ones; 'neutral' ends it. */
  express(name, until) {
    this.shown = EXPRESSIONS[name] && name !== 'neutral' ? name : null;
    this.shownUntil = this.shown ? until : 0;
  }

  /** Her standing mood: name from mind/mood.py (or null), intensity 0..1 (how far from calm). */
  setBase(name, intensity = 0.5) {
    this.base = EXPRESSIONS[name] ? name : null;
    this.baseStrength = 0.55 + 0.45 * Math.max(0, Math.min(1, intensity));
  }

  /** inp: {state, mouth (or null), pointer: [yaw, pitch] radians or null, cfg} */
  update(t, dt, inp) {
    dt = Math.min(dt, 0.1);
    const cfg = inp.cfg;
    const expressive = cfg.expressiveness ?? 1, motion = cfg.motion ?? 1;
    const state = inp.state;
    if (state !== this.lastState) {
      if (state === 'thinking') this.thinkSide = Math.random() < 0.5 ? -1 : 1;
      this.lastState = state;
    }
    const w = this.w;
    w.fill(0);

    // --- expression: state (or mood while speaking), spring-smoothed --------------------------------
    const moodActive = t < this.moodUntil;
    const shown = this.shown && t < this.shownUntil && state !== 'asleep' ? this.shown : null;
    let name = shown || STATE_EXPR[state] || (moodActive ? this.mood : (this.base || 'neutral'));
    if (!shown && state === 'speaking' && moodActive) name = this.mood;
    const target = EXPRESSIONS[name] || {};
    const scale = name === 'asleep' ? 1 : shown ? Math.max(expressive, 0.6)
      : expressive * (name === this.base && !moodActive ? this.baseStrength : 1);
    const mm = (state !== 'asleep' && this.base && MOOD_MOTION[this.base]) || {};
    this.energy = approach(this.energy, mm.energy ?? 1, 0.5, dt);
    this.droop = approach(this.droop, mm.droop ?? 0, 0.5, dt);
    this.tilt = approach(this.tilt, mm.tilt ?? 0, 0.5, dt);
    for (let c = 0; c < CH.length; c++) {
      const v = (target[CH[c]] || 0) * (CH[c].startsWith('blink') ? 1 : scale);
      this.expr[c] = approach(this.expr[c], v, name === 'asleep' ? 2.5 : 6, dt);
      w[c] = this.expr[c];
    }
    this.exprJaw = approach(this.exprJaw, (target.jaw || 0) * scale, 6, dt);

    // --- lip sync ---------------------------------------------------------------------------------------
    const m = this.mouth, src = inp.mouth;
    const gain = cfg.mouth_gain ?? 1;
    for (const k of Object.keys(m)) {
      const v = src ? (src[k] || 0) : 0;
      m[k] = approach(m[k], v, v > m[k] ? 140 : 70, dt);     // barely any: frames are already smooth, and lag shows
    }
    const talking = m.energy > 0.05 || m.jaw > 0.02;
    let jawSleep = 0;
    const jawOf = () => Math.min(1, m.jaw * gain + this.exprJaw * (1 - m.close) + jawSleep);
    w[C.close] = Math.max(w[C.close] * (1 - m.jaw * 2), m.close);
    w[C.wide] += Math.min(1, m.wide * 0.9 * gain + m.fric * 0.14952);
    w[C.round] += Math.min(1, m.round * gain);
    w[C.upperUp] += m.fric * 0.24973 + m.jaw * 0.12 * gain;
    w[C.lowerDown] += m.jaw * 0.21931 * gain;
    w[C.bite] += m.bite;
    if (talking) {                                             // mouth shapes win over the expression's mouth
      const k = Math.min(1, (m.energy + m.jaw) * 2);
      for (const c of ['pout', 'frown']) w[C[c]] *= 1 - 0.7015 * k;
    }

    // --- sleep: slow breathing; waking up lifts the brows and widens the eyes for a moment ---------------------
    const wasAsleep = this.sleep > 0.5;
    this.sleep = approach(this.sleep, state === 'asleep' ? 1 : 0, state === 'asleep' ? 0.9 : 2.5, dt);
    const breath = Math.sin(t * 1.3469);                       // ~13 breaths a minute
    if (this.sleep > 0.01) {
      jawSleep = Math.max(0, -breath) * 0.06 * this.sleep;      // the lips part a little on each breath out
      w[C.browInner] += 0.04 * (1 + breath) * this.sleep;
    }
    if (wasAsleep && this.sleep <= 0.5) {
      this.gestures.push({ g: { dur: 1.1, ch: { browUpL: 0.45, browUpR: 0.45, eyeWideL: 0.35, eyeWideR: 0.35 } }, t0: t, s: 1 });
      this.nextBlink = t + 0.4;                               // and a blink as the eyes open
    }

    // --- blinks --------------------------------------------------------------------------------------------
    if (this.blinkT < 0 && t > this.nextBlink && state !== 'asleep') {
      this.blinkT = 0;
      this.doubleBlink = Math.random() < 0.12047;
      this.nextBlink = t + 2.2 + Math.random() * 4.1801;
    }
    if (this.blinkT >= 0) {
      this.blinkT += dt;
      const dur = 0.16051;
      let ph = this.blinkT / dur;
      if (this.doubleBlink && ph >= 1 && ph < 2.2) ph = Math.max(0, ph - 1.2);
      const a = ph < 1 ? Math.sin(Math.min(1, ph) * Math.PI) ** 0.7979 : 0;
      if (this.blinkT > dur * (this.doubleBlink ? 2.4 : 1)) this.blinkT = -1;
      w[C.blinkL] = Math.max(w[C.blinkL], a);
      w[C.blinkR] = Math.max(w[C.blinkR], a);
    }

    // --- micro gestures (idle life) ------------------------------------------------------------------------
    if (t > this.nextGesture && state !== 'asleep' && expressive > 0.05) {
      const pool = GESTURES.filter(g => !talking || g.talkSafe);
      const g = pool[Math.floor(Math.random() * pool.length)];
      this.gestures.push({ g, t0: t, s: (0.6 + Math.random() * 0.6) * expressive });
      this.nextGesture = t + ((talking ? 5 : 4) + Math.random() * 8) / Math.max(0.3, this.energy);
    }
    this.gestures = this.gestures.filter(x => t - x.t0 < x.g.dur);
    for (const x of this.gestures) {
      const v = Math.sin(Math.PI * (t - x.t0) / x.g.dur) * x.s;
      for (const [ch, a] of Object.entries(x.g.ch)) w[C[ch]] += a * v;
    }
    // speech emphasis: brows lift on stressed syllables
    this.emph = approach(this.emph, m.energy > 0.7 && m.jaw > 0.35 ? 1 : 0, 5, dt);
    w[C.browUpL] += 0.12 * this.emph * expressive;
    w[C.browUpR] += 0.12 * this.emph * expressive;

    // --- gaze -------------------------------------------------------------------------------------------------
    if (t > this.nextSaccade) {
      this.nextSaccade = t + (state === 'listening' ? 1.2 : 0.5) + Math.random() * 2.2;
      this.saccade = [(Math.random() - 0.5) * 0.12, (Math.random() - 0.5) * 0.06977];
    }
    let gy = this.saccade[0], gp = this.saccade[1];
    const ptr = cfg.follow_mouse !== false && inp.pointer ? inp.pointer : null;
    if (state === 'thinking') { gy += 0.3 * this.thinkSide; gp += 0.2; }
    else if (state === 'asleep') { gp -= 0.25; }
    else if (ptr) { gy += Math.max(-0.5, Math.min(0.5, ptr[0])); gp += Math.max(-0.35, Math.min(0.35, ptr[1])); }
    this.gaze[0] = approach(this.gaze[0], gy, 22, dt);
    this.gaze[1] = approach(this.gaze[1], gp, 22, dt);
    // looking down lowers the lids a little
    const down = Math.max(0, -this.gaze[1]);
    w[C.blinkL] += down * 0.5; w[C.blinkR] += down * 0.5;

    // --- head -----------------------------------------------------------------------------------------------
    const follow = ptr ? 0.35 : 0.15;
    const sway = motion * (0.6 + 0.4 * this.energy);
    let yaw = noise(t, 1) * 0.05 * sway + this.gaze[0] * follow;
    let pitch = noise(t, 2) * 0.035082 * sway + this.gaze[1] * follow * 0.6 - this.droop;
    let roll = noise(t, 3) * 0.025085 * sway + this.tilt;
    if (state === 'thinking') { roll += 0.07 * this.thinkSide; pitch += 0.04; }
    if (state === 'listening') { pitch -= 0.03; roll += 0.03; }
    // asleep: the head drops and leans, rising a little with each breath in
    pitch -= (0.16 - 0.025 * breath) * this.sleep;
    roll += 0.06 * this.sleep;
    pitch -= (m.energy * 0.03 + this.emph * 0.025) * motion;                 // nods with the speech
    yaw += Math.sin(t * 1.7) * m.energy * 0.011998 * motion;
    const hr = 4;
    this.head.yaw = approach(this.head.yaw, yaw, hr, dt);
    this.head.pitch = approach(this.head.pitch, pitch, hr, dt);
    this.head.roll = approach(this.head.roll, roll, hr, dt);
    this.head.y = Math.sin(t * 1.2964) * 0.06 * motion * (1 - this.sleep) + breath * 0.1 * this.sleep;

    // --- pupils, brightness --------------------------------------------------------------------------------------
    const pupilTarget = state === 'thinking' ? 0.23 : name === 'surprised' ? 0.26926 : 0.2 + noise(t * 0.5, 9) * 0.015;
    this.pupil = approach(this.pupil, pupilTarget, 3, dt);
    const dimTarget = state === 'asleep' ? 0.5 + 0.06 * breath : state === 'muted' ? 0.85 : 1;   // the glow breathes too
    this.dim = approach(this.dim, dimTarget, 2, dt);

    for (let c = 0; c < CH.length; c++) w[c] = Math.max(-0.2, Math.min(1.15, w[c]));
    return {
      weights: w, jaw: jawOf(), head: this.head, gaze: this.gaze, pupil: this.pupil, dim: this.dim,
      tongue: [m.tongue * 0.38081, m.tongue * 0.8], talk: Math.min(1, m.energy * 1.5),
    };
  }
}
