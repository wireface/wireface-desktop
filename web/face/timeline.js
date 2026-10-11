/*! Wireface | Required Notice: Copyright 2026 Compsmart (https://wireface.dev) | Wireface SDK Licence; free local use including commercial: https://wireface.dev/license/ */
// Lip-sync timeline. The actor sends mouth frames keyed by the audio player's sample index
// (ahead of playback) and a clock: "sample s reaches the speaker at wall time t". So the frame to
// show now is the one at s + (now - t) * sr, whatever the device latency. A newer packet for the
// same samples (e.g. a refined alignment) replaces an older one.
export class MouthTimeline {
  constructor() {
    this.packets = [];
    this.clock = null;
  }

  add(p) {
    const n = p.frames.length;
    if (!n) return;
    const idx = Object.fromEntries(p.ch.map((name, i) => [name, i]));
    this.packets.push({ s0: p.s0, hop: p.hop, sr: p.sr, end: p.s0 + n * p.hop, frames: p.frames, idx, src: p.src, text: p.text });
    // Nothing is pruned here: speech is synthesized far faster than it plays, so a long reply's packets
    // arrive many seconds ahead, and dropping the ones well behind the newest shut the mouth for audio
    // that hadn't been heard yet. This is only a memory guard, far above any real lead.
    if (this.packets.length > 6000) this.packets.splice(0, this.packets.length - 6000);
  }

  setClock(c) {
    this.clock = { s: c.s, t: c.t, sr: c.sr, end: c.end ?? null };
    // Prune what has been played, by the clock reading itself (only sent while audio is flowing), never by
    // the clock run forward: a reply's first packet usually arrives a moment before its clock, and the
    // previous reply's clock, run on through the silence since, made every new packet look stale.
    this.packets = this.packets.filter(q => q.end > c.s - q.sr);
  }

  flush() { this.packets = []; this.clock = null; }

  /** sample index reaching the speaker at wall time ``now`` (seconds), or null */
  index(now) {
    const c = this.clock;
    return c ? c.s + (now - c.t) * c.sr : null;
  }

  /** mouth channels at wall time ``now`` (seconds), or null when nothing is audible */
  sample(now) {
    const c = this.clock;
    if (!c) return null;
    const s = c.s + (now - c.t) * c.sr;
    if (c.end != null && s >= c.end) return null;
    for (let i = this.packets.length - 1; i >= 0; i--) {
      const p = this.packets[i];
      if (s < p.s0 || s >= p.end) continue;
      const x = (s - p.s0) / p.hop;
      const k = Math.floor(x), u = x - k;
      const a = p.frames[k], b = p.frames[Math.min(k + 1, p.frames.length - 1)];
      const out = {};
      for (const [name, j] of Object.entries(p.idx)) out[name] = a[j] + (b[j] - a[j]) * u;
      out.src = p.src;
      out.text = p.text;
      return out;
    }
    return null;
  }
}
