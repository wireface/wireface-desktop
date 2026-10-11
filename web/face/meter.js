// The voice detector ("show voice detector", Settings > Voice > Advanced): how far what the microphone hears stands out
// from what the room usually sounds like (wireface/surprise.py), one bar per 50 ms block, scrolling right to left, with
// what became of each utterance drawn over it. The face window shows a small one and the settings a larger one.
// Feed it the backend's "meter" messages with push(); it draws itself on every animation frame while its canvas is on
// the page. Colours come from the canvas's CSS: --vm-strong, --vm-some, --vm-low, --vm-bad, --vm-would, --vm-text,
// --vm-muted, --vm-grid, --vm-deaf.

export const STRONG_DB = 12;           // as surprise.py: this far above the room's usual loud level, it stands out strongly
export const SOME_DB = 6;              // somewhat; less: it sounds like the room
const LO = -24, HI = 36;               // the dB range drawn
const BLOCK_S = 0.05;
const STALE_S = 1.5;                   // nothing new for this long: the microphone isn't being read

export const FATES = {
  heard: 'heard', would: 'would ignore', background: 'ignored', distant: 'too far', empty: 'no words',
};

const signed = db => (db == null ? '?' : `${db > 0 ? '+' : ''}${Math.round(db)}`);

export class VoiceMeter {
  /** ``seconds``: how much is shown; ``compact``: the small one on the face (fewer words). */
  constructor(canvas, { seconds = 10, compact = false } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.seconds = seconds;
    this.compact = compact;
    this.blocks = [];                  // { t, lift, speech, deaf }
    this.marks = [];                   // utterances and the name, as hearing.Ears records them
    this.offset = null;                // page clock - backend clock (seconds)
    this.lastAt = 0;
    this.roomReady = false;
    this.gate = false;
    this.idle = '';                    // said when nothing is coming in ("Not listening")
    this.onUtterance = null;           // (mark) => void, for a list of what was heard
    this._colors = null;
    this._colorsAt = 0;
    this._raf = 0;
  }

  push(msg) {
    const arrival = performance.now() / 1000;
    const last = msg.blocks?.length ? msg.blocks[msg.blocks.length - 1][0] : null;
    if (last != null) {
      const o = arrival - last;
      // the smallest delay seen is the truest; drift up slowly, and start again after a restart of the backend
      this.offset = this.offset == null || o < this.offset || Math.abs(o - this.offset) > 2 ? o : this.offset + (o - this.offset) * 0.05;
    }
    for (const [t, lift, speech, deaf] of msg.blocks || []) this.blocks.push({ t, lift, speech: !!speech, deaf: !!deaf });
    for (const m of msg.marks || []) {
      this.marks.push(m);
      if (m.kind === 'utterance') this.onUtterance?.(m);
    }
    this.roomReady = !!msg.room_ready;
    this.gate = !!msg.gate;
    this.lastAt = arrival;
    const newest = this.blocks.length ? this.blocks[this.blocks.length - 1].t : 0;
    const keep = newest - this.seconds - 2;
    let i = 0;
    while (i < this.blocks.length && this.blocks[i].t < keep) i++;
    if (i) this.blocks.splice(0, i);
    if (this.marks.length > 60) this.marks.splice(0, this.marks.length - 60);
  }

  clear() {
    this.blocks = [];
    this.marks = [];
  }

  /** The latest reading (dB above the room), or null. */
  get now() {
    for (let i = this.blocks.length - 1; i >= 0 && i > this.blocks.length - 5; i--) if (this.blocks[i].lift != null) return this.blocks[i].lift;
    return null;
  }

  start() {
    if (this._raf) return;
    const loop = () => {
      this._raf = 0;
      if (!this.canvas.isConnected) return;
      this.draw();
      this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }

  stop() {
    cancelAnimationFrame(this._raf);
    this._raf = 0;
  }

  colors() {
    const t = performance.now();
    if (!this._colors || t - this._colorsAt > 1000) {
      const cs = getComputedStyle(this.canvas);
      const v = name => cs.getPropertyValue(`--vm-${name}`).trim() || '#888';
      this._colors = Object.fromEntries(['strong', 'some', 'low', 'bad', 'would', 'text', 'muted', 'grid', 'deaf'].map(k => [k, v(k)]));
      this._colorsAt = t;
    }
    return this._colors;
  }

  draw() {
    const c = this.canvas, ctx = this.ctx, dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.clientHeight;
    if (!w || !h) return;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const col = this.colors(), small = this.compact;
    const font = px => `${px}px "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif`;
    const padL = 6, padR = small ? 30 : 46, padT = small ? 17 : 22, padB = 5;
    const pw = w - padL - padR, ph = h - padT - padB;
    const clock = performance.now() / 1000;
    const now = clock - (this.offset ?? 0);
    const x = t => padL + pw * (1 - (now - t) / this.seconds);
    const y = db => padT + ph * (1 - (Math.max(LO, Math.min(HI, db)) - LO) / (HI - LO));

    // the zones: stands out strongly, somewhat, sounds like the room
    ctx.globalAlpha = 0.08;
    ctx.fillStyle = col.strong;
    ctx.fillRect(padL, padT, pw, y(STRONG_DB) - padT);
    ctx.fillStyle = col.some;
    ctx.fillRect(padL, y(STRONG_DB), pw, y(SOME_DB) - y(STRONG_DB));
    ctx.globalAlpha = 1;
    ctx.font = font(small ? 9.5 : 11);
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    for (const [db, label] of [[STRONG_DB, '+12'], [SOME_DB, '+6'], [0, 'room']]) {
      ctx.strokeStyle = col.grid;
      ctx.lineWidth = 1;
      ctx.setLineDash(db ? [3, 3] : []);
      ctx.beginPath();
      ctx.moveTo(padL, Math.round(y(db)) + 0.5);
      ctx.lineTo(padL + pw, Math.round(y(db)) + 0.5);
      ctx.stroke();
      ctx.fillStyle = db === STRONG_DB ? col.strong : db === SOME_DB ? col.some : col.muted;
      if (!(small && db === SOME_DB)) ctx.fillText(label, padL + pw + 5, y(db));     // (too close to +12 on the small one)
    }
    ctx.setLineDash([]);

    // one bar per block: bright while the voice detector hears speech; a band where the face talked (it is deaf then)
    const bw = Math.max(1, pw / (this.seconds / BLOCK_S));
    for (const b of this.blocks) {
      const bx = x(b.t) - bw;
      if (bx + bw < padL || bx > padL + pw) continue;
      if (b.deaf) {
        ctx.globalAlpha = 0.16;
        ctx.fillStyle = col.deaf;
        ctx.fillRect(bx, padT, bw + 0.5, ph);
        continue;
      }
      if (b.lift == null) continue;
      ctx.globalAlpha = b.speech ? 0.95 : 0.32;
      ctx.fillStyle = b.lift >= STRONG_DB ? col.strong : b.lift >= SOME_DB ? col.some : col.low;
      const top = y(b.lift);
      ctx.fillRect(bx, top, bw + 0.5, padT + ph - top);
    }
    ctx.globalAlpha = 1;

    // what became of each utterance: a line at its level, across its length; where the name was heard
    ctx.save();
    ctx.beginPath();
    ctx.rect(padL, 0, pw, h);
    ctx.clip();
    ctx.textBaseline = 'alphabetic';
    let labelRight = -Infinity;
    for (const m of this.marks) {
      if (m.kind === 'name') {
        const nx = x(m.t);
        ctx.strokeStyle = col.text;
        ctx.globalAlpha = 0.6;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(nx, padT);
        ctx.lineTo(nx, padT + ph);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
        continue;
      }
      if (m.lift == null) continue;
      const x0 = x(m.t - m.dur), x1 = x(m.t), my = y(m.lift);
      if (x1 < padL || x0 > padL + pw) continue;
      const fc = m.fate === 'heard' ? col.text : m.fate === 'would' ? col.would : m.fate === 'background' ? col.bad : col.muted;
      ctx.strokeStyle = fc;
      ctx.lineWidth = small ? 2 : 2.5;
      ctx.beginPath();
      ctx.moveTo(x0, my);
      ctx.lineTo(x1, my);
      ctx.stroke();
      ctx.lineWidth = 1;
      const label = small ? `${signed(m.lift)} ${m.fate === 'heard' ? '' : FATES[m.fate]}`.trim() : `${signed(m.lift)} dB ${FATES[m.fate] || m.fate}`;
      ctx.font = font(small ? 9.5 : 11);
      const lw = ctx.measureText(label).width;
      const lx = Math.max(padL + 2, Math.min(x0, padL + pw - lw - 2));
      if (lx > labelRight + 4) {                     // labels that would overlap the one before are left off
        ctx.fillStyle = fc;
        ctx.fillText(label, lx, Math.max(padT + 10, my - 5));
        labelRight = lx + lw;
      }
    }
    ctx.restore();

    // the heading and the reading now
    ctx.textBaseline = 'alphabetic';
    ctx.font = font(small ? 10 : 12);
    ctx.textAlign = 'left';
    ctx.fillStyle = col.muted;
    const title = small ? 'Stands out from the room' : 'How far what it hears stands out from the room (dB)';
    ctx.fillText(title, padL, padT - (small ? 5 : 8));
    const live = clock - this.lastAt < STALE_S;
    const reading = this.now;
    ctx.textAlign = 'right';
    if (live && this.roomReady && reading != null) {
      ctx.fillStyle = reading >= STRONG_DB ? col.strong : reading >= SOME_DB ? col.some : col.text;
      ctx.font = font(small ? 10.5 : 12.5);
      ctx.fillText(`${signed(reading)} dB`, w - 4, padT - (small ? 5 : 8));
    }
    // why there is nothing to see
    const why = !live ? this.idle || 'Waiting for the microphone...' : !this.roomReady ? 'Learning what the room sounds like...' : '';
    if (why) {
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = font(small ? 10.5 : 12.5);
      ctx.fillStyle = col.text;
      ctx.fillText(why, padL + pw / 2, padT + ph / 2);
    }
  }
}
