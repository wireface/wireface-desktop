// The Face tab: how the face looks and moves (face.json through /api/face), changed live. Sliders and colour wells
// apply locally at once and are saved a few times a second while they move, so the face window follows them from the
// broadcast. The face always wears a photo over its wireframe, and the Wireframe <-> Skin slider says how much of it.
// A built-in portrait is put on by the server (its landmarks come with it); a photo of your own is mapped here
// (MediaPipe finds the face in the picture) and sent to the server.
import { $, h, send, SHELL, TOKEN } from './common.js';
import { group, row, toggle, seg, slider, colorWell, btn, note, icon, svg, ICONS, keepFocus } from './ui.js';
import { preparePhoto } from '../face/photo.js';
import { irisColour } from '../face/render.js';

let data = null;                 // GET /api/face: presets, defaults, portraits, expressions, ...
let cfg = null;                  // the face config, with any change made here applied
let toast = () => {};
let applying = null;             // portrait id (or 'upload') while a picture is being put on
let photoError = '';
let photo = { version: null, url: null, iris: null };   // the current photo as an object URL, and its own eye colour
let pending = {}, timer = 0, inflight = 0, dragging = false, unsettled = false;
const playing = new Map();       // expression -> timer, while its preview runs

const root = () => $('tab-face');
const isPhoto = () => !!cfg?.photo;
const skin = () => cfg.skin_amount ?? 1;

export async function init(toastFn) {
  toast = toastFn;
  await reload();
  root().addEventListener('pointerdown', e => { if (e.target.type === 'range') dragging = true; });
  addEventListener('pointerup', () => { if (dragging) { dragging = false; settle(); } });
  root().addEventListener('scroll', spy, { passive: true });
  return cfg;
}

async function reload() {
  data = await send('/api/face');
  cfg = data.config;
  loadPhoto();
  render();
}

/** A face_config broadcast: someone (this tab, the menu, the agent) changed the look. */
export function config(c) {
  if (!cfg) return;
  if (busy()) return;                                  // our own newer change is still on its way
  if (JSON.stringify(c) === JSON.stringify(cfg)) return;
  cfg = c;
  loadPhoto();
  render();
}

export function shown(anchor) {
  // portraits may have been added since: check again
  send('/api/face').then(d => {
    const fresh = JSON.stringify(d.portraits) !== JSON.stringify(data.portraits);
    data = { ...d, config: cfg };
    if (fresh) render();
  }).catch(() => {});
  render();
  if (anchor) jump(anchor);
}

// --- saving -----------------------------------------------------------------------------------------------------------
// One request at a time: a control on the move is saved every LIVE_MS or so (the face follows it), and when let go.
const LIVE_MS = 80;
const busy = () => dragging || inflight > 0 || timer || Object.keys(pending).length;
const merge = (a, b) => ({ ...a, ...b, ...(b.colors ? { colors: { ...a.colors, ...b.colors } } : {}) });

/** Change the look: at once here, saved now (done) or a few times a second while the control moves. */
function put(patch, done = true) {
  const local = patch.preset ? { colors: { ...data.presets[patch.preset] } } : patch;
  cfg = merge(cfg, local);
  pending = merge(pending, patch);
  if (inflight) return;                                // sent once the one on its way is back
  if (done) clearTimeout(timer);
  else if (timer) return;
  timer = setTimeout(flush, done ? 0 : LIVE_MS);
}

async function flush() {
  timer = 0;
  const patch = pending;
  pending = {};
  if (!Object.keys(patch).length) return;
  inflight++;
  let saved = null;
  try {
    saved = (await send('/api/face', 'PUT', patch)).config;
  } catch (e) {
    toast(e.message);
    saved = (await send('/api/face').catch(() => null))?.config || null;
  }
  inflight--;
  if (Object.keys(pending).length) { timer = setTimeout(flush, LIVE_MS); return; }   // what changed meanwhile
  if (saved && !busy()) {
    const changed = JSON.stringify(saved) !== JSON.stringify(cfg);
    cfg = saved;
    if (changed || unsettled) { unsettled = false; render(); }
  }
}

/** A control was let go: redraw (what depends on its value), now or once its last change is saved. */
function settle() {
  if (!busy()) { unsettled = false; render(); } else unsettled = true;
}

// --- the photo ------------------------------------------------------------------------------------------------------
// Which portrait the photo is (the server knows; for a photo put on before it did, this panel may remember), or 'upload'.
const sources = () => { try { return JSON.parse(localStorage.getItem('face.photoSources') || '{}'); } catch { return {}; } };
const currentSource = () => (cfg?.photo ? cfg.photo.portrait || sources()[cfg.photo.version] || 'upload' : null);

async function loadPhoto() {
  const v = cfg?.photo?.version || null;
  if (v === photo.version) return;
  if (photo.url) URL.revokeObjectURL(photo.url);
  photo = { version: v, url: null, iris: null };
  if (!v) return;
  try {
    const r = await fetch(`/api/face/photo?v=${v}`, { headers: { 'X-Face-Token': TOKEN } });
    if (!r.ok) return;
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    if (photo.version !== v) { URL.revokeObjectURL(url); return; }
    photo.url = url;
    render();
    const iris = await ownIris(v, blob);
    if (photo.version === v && iris) { photo.iris = iris; render(); }
  } catch { /* no preview: the gallery still works */ }
}

/** The photo's own eye colour as the face finds it (the same reading of the same picture), #rrggbb; null if none. */
async function ownIris(v, blob) {
  const r = await fetch(`/api/face/landmarks?v=${v}`, { headers: { 'X-Face-Token': TOKEN } });
  if (!r.ok) return null;
  const [landmarks, bitmap] = await Promise.all([r.json(), createImageBitmap(blob)]);
  try {
    const c = irisColour(bitmap, landmarks);
    return c && `#${c.map(x => Math.round(x * 255).toString(16).padStart(2, '0')).join('')}`;
  } finally {
    bitmap.close();
  }
}

/** Wear a picture: a portrait by its id (no picture: the server has its landmarks), or one whose face is found here. */
async function wear(blob, source, label) {
  if (applying) return;
  applying = source;
  photoError = '';
  render();
  try {
    const portrait = source === 'upload' ? {} : { portrait: source };
    const body = blob ? { ...(await preparePhoto(blob)), ...portrait } : portrait;
    cfg = (await send('/api/face/photo', 'PUT', body)).config;
    loadPhoto();
    toast(`${label} is now the face.`);
  } catch (e) {
    photoError = `${label}: ${e.message}`;
  }
  applying = null;
  render();
}

async function usePortrait(p) {
  if (applying || currentSource() === p.id) return;
  if (p.landmarks) { await wear(null, p.id, p.label); return; }
  try {                          // added without its landmarks (scripts/portrait-landmarks.py makes them): find them here
    const r = await fetch(p.image);
    if (!r.ok) throw new Error(`could not load it (${r.status})`);
    await wear(await r.blob(), p.id, p.label);
  } catch (e) {
    photoError = `${p.label}: ${e.message}`;
    render();
  }
}

function chooseFile() {
  const input = h('input', { type: 'file', accept: 'image/*', hidden: true });
  input.addEventListener('change', () => { const f = input.files[0]; input.remove(); if (f) wear(f, 'upload', 'Your photo'); });
  document.body.append(input);
  input.click();
}

// --- little drawings ------------------------------------------------------------------------------------------------
const HEAD = 'M32 5C45 5 53 15 53 29c0 16-9 31-21 37C20 60 11 45 11 29 11 15 19 5 32 5z';

function wireframeArt() {
  const lines = [HEAD, 'M16 17q16 5 32 0', 'M11.5 29q20.5 7 41 0', 'M13 41q19 6.5 38 0', 'M19 53q13 4.5 26 0',
    'M32 5c-9 10-11 30-8 45 1.5 7 4 12 8 16', 'M32 5c9 10 11 30 8 45-1.5 7-4 12-8 16', 'M32 5v61',
    'M21 31q4.5-3 9 0q-4.5 3-9 0z', 'M34 31q4.5-3 9 0q-4.5 3-9 0z', 'M25 50q7 3.5 14 0'];
  return svg('svg', { viewBox: '0 0 64 72', class: 'holo' }, ...lines.map((d, i) => svg('path', { d, opacity: i === 0 || i > 7 ? 1 : 0.5 })));
}

function photoArt() {
  if (photo.url) return h('img', { src: photo.url, alt: '' });
  return h('div', { class: 'ph' }, icon(ICONS.person));
}

// expression glyphs: a round face whose eyes, brows and mouth say which one it is
const GLYPHS = {
  neutral: {}, happy: { m: 3 }, excited: { grin: 1, raise: 1.2, eyes: 'wide' }, amused: { m: 2.6, eyes: 'arc' },
  surprised: { o: 1, raise: 1.8, eyes: 'wide' }, curious: { m: 1, browR: 1.6 }, thinking: { side: 1, browL: 1.6, look: 1 },
  concerned: { m: -1, tilt: 1 }, sad: { m: -2.6, tilt: 1.5 }, afraid: { wavy: 1, tilt: 1.5, raise: 1, eyes: 'wide' },
  frustrated: { m: -1.2, tilt: -1.6 }, calm: { m: 1.6, eyes: 'closed' },
};

function glyph(name) {
  const f = GLYPHS[name] || {};
  const parts = [svg('circle', { cx: 12, cy: 12, r: 9.5, class: 'g-head' })];
  for (const x of [9, 15]) {
    if (f.eyes === 'arc') parts.push(svg('path', { d: `M${x - 1.6} 11.2q1.6-2.2 3.2 0` }));
    else if (f.eyes === 'closed') parts.push(svg('path', { d: `M${x - 1.6} 10.4q1.6 1.8 3.2 0` }));
    else parts.push(svg('circle', { cx: x + (f.look ? 0.6 : 0), cy: f.look ? 9.9 : 10.6, r: f.eyes === 'wide' ? 1.35 : 0.95, class: 'g-fill' }));
  }
  const y = 7.4 - (f.raise || 0) * 0.7, tilt = (f.tilt || 0) * 0.8;
  parts.push(svg('path', { d: `M7 ${y - (f.browL || 0) * 0.5}L10.6 ${y - tilt - (f.browL || 0)}` }));
  parts.push(svg('path', { d: `M13.4 ${y - tilt - (f.browR || 0)}L17 ${y - (f.browR || 0) * 0.5}` }));
  let mouth;
  if (f.o) mouth = svg('ellipse', { cx: 12, cy: 16.1, rx: 1.6, ry: 2 });
  else if (f.grin) mouth = svg('path', { d: 'M8.4 14.6h7.2q-.6 4.2-3.6 4.2t-3.6-4.2z', class: 'g-soft' });
  else if (f.wavy) mouth = svg('path', { d: 'M8.6 16.4q.85-1 1.7 0t1.7 0t1.7 0t1.7 0' });
  else if (f.side) mouth = svg('path', { d: 'M9.6 16.6l5.2-1' });
  else mouth = svg('path', { d: `M8.5 15.6q3.5 ${(f.m || 0) * 1.05} 7 0` });
  parts.push(mouth);
  return svg('svg', { viewBox: '0 0 24 24', class: 'glyph', 'aria-hidden': 'true' }, ...parts);
}

// --- groups ---------------------------------------------------------------------------------------------------------
const skinFmt = v => (v <= 0 ? 'Wireframe' : v >= 1 ? 'All skin' : `${pct(v)} skin`);

/** Wireframe <-> Skin: how far down the face the photo covers the glowing wireframe. */
function skinGroup() {
  const v = skin();
  const to = x => () => { if (skin() !== x) { put({ skin_amount: x }); render(); } };
  const s = slider('Wireframe \u2194 Skin', { value: v, min: 0, max: 1, step: 0.01, fmt: skinFmt, reset: data.defaults.skin_amount,
    k: 'skin_amount', help: 'Slide towards Wireframe to fade the skin into the glowing wireframe underneath. In between, the face is a blend of both.' },
  (x, done) => { put({ skin_amount: x }, done); if (done) settle(); });         // the ends and Photo face follow it
  s.append(h('div', { class: 'ends' },
    h('button', { class: `end ${v <= 0 ? 'on' : ''}`, title: 'All wireframe', dataset: { k: 'skin:0' }, onclick: to(0) },
      h('span', { class: 'eart wire' }, wireframeArt()), 'Wireframe'),
    h('button', { class: `end ${v >= 1 ? 'on' : ''}`, title: 'All skin', dataset: { k: 'skin:1' }, onclick: to(1) },
      'Skin', h('span', { class: 'eart' }, photoArt()))));
  return group({ title: 'Skin', id: 'f-skin' }, s);
}

function photoGroup() {
  const src = currentSource(), list = data.portraits || [];
  const named = list.find(p => p.id === src);
  const title = !isPhoto() ? 'No photo' : named ? named.label : 'Your photo';
  const hidden = isPhoto() && skin() <= 0;
  const sub = !isPhoto() ? 'Pick a portrait, or use a photo of your own.'
    : hidden ? 'Not showing: the face is all wireframe.' : 'Wearing it now.';

  const current = h('div', { class: 'current' },
    h('span', { class: 'cthumb' }, photoArt()),
    h('div', { class: 'ctext' }, h('strong', {}, title), h('small', {}, sub)),
    hidden ? btn('Show', () => { put({ skin_amount: 1 }); render(); }, { kind: 'primary sm', title: 'Slide the skin all the way on' }) : null);

  const tile = p => {
    const on = isPhoto() && src === p.id, busy = applying === p.id;
    return h('button', { class: `portrait ${on ? 'on' : ''} ${busy ? 'busy' : ''}`, 'aria-pressed': String(on), title: p.label,
      disabled: (applying && !busy) || null, dataset: { k: `portrait:${p.id}` }, onclick: () => usePortrait(p) },
    h('img', { src: p.thumb, alt: '', loading: 'lazy', draggable: 'false' }),
    h('span', { class: 'plabel' }, p.label),
    on ? h('span', { class: 'tick' }, icon(ICONS.check)) : null,
    busy ? h('span', { class: 'veil' }, h('span', { class: 'spin' })) : null);
  };
  const upload = h('button', { class: `portrait upload ${applying === 'upload' ? 'busy' : ''} ${src === 'upload' && isPhoto() ? 'on' : ''}`,
    disabled: (applying && applying !== 'upload') || null, dataset: { k: 'portrait:upload' }, onclick: chooseFile },
  applying === 'upload' ? h('span', { class: 'spin' }) : icon(ICONS.upload), h('span', { class: 'ulabel' }, 'Your own photo'));

  const empty = !list.length ? h('div', { class: 'empty-portraits' },
    h('div', { class: 'ghosts', 'aria-hidden': 'true' }, h('span'), h('span'), h('span')),
    h('div', {}, h('strong', {}, 'No portraits installed yet'),
      h('small', {}, 'Built-in photorealistic portraits appear here once they are added. Your own photo works now.')),
    h('button', { class: 'iconbtn', title: 'Look again', 'aria-label': 'Look for portraits again', onclick: () => shown() }, icon(ICONS.refresh))) : null;

  const status = applying
    ? note('busy', `Mapping ${applying === 'upload' ? 'your photo' : (list.find(p => p.id === applying)?.label || 'the portrait')} onto the face...`,
      h('div', { class: 'bar indeterminate' }, h('span')))
    : photoError ? note('bad', photoError) : null;

  return group({ title: 'Photo face', id: 'f-photo', note: 'The face the skin shows. Any face-shaped picture works: a robot or a monster too.' },
    current,
    h('div', { class: 'gallery' }, upload, list.map(tile)),
    empty, status,
    teethBlock(),
    eyesBlock());
}

function teethBlock() {
  return h('div', { class: 'teeth' },
    h('div', { class: 'subhead' }, 'Teeth', h('small', {}, 'when the mouth opens')),
    h('div', { class: 'teethgrid', role: 'group', 'aria-label': 'Teeth' }, data.teeth_styles.map(id => {
      const on = (cfg.teeth_style || 'normal') === id;
      return h('button', { class: `tooth ${on ? 'on' : ''}`, 'aria-pressed': String(on), dataset: { k: `teeth:${id}` },
        onclick: () => { if (!on) { put({ teeth_style: id }); render(); } } },
      h('img', { src: `/static/panel/img/teeth-${id}.png`, alt: '', draggable: 'false' }), h('span', {}, TEETH_LABELS[id] || id));
    })));
}
const TEETH_LABELS = { normal: 'Normal', braces: 'Braces', vampire: 'Vampire', goofy: 'Goofy', sharp: 'Sharp', turkey: 'Turkey' };

// eye colours to pick from, besides the photo's own and any other (the eyeball shades each towards its rim)
const IRISES = [['brown', 'Brown', '#6b4428'], ['hazel', 'Hazel', '#8d6b3c'], ['amber', 'Amber', '#c28a2c'], ['green', 'Green', '#5b8c4a'],
  ['blue', 'Blue', '#4f86c6'], ['grey', 'Grey', '#8a98a5'], ['violet', 'Violet', '#8b6cc7'], ['red', 'Red', '#c0352b']];

/** The skin's eye colour: the photo's own (as the face finds it), one of IRISES, or any colour. */
function eyesBlock() {
  const own = cfg.iris_from_photo !== false, iris = cfg.colors.iris;
  const custom = !own && !IRISES.some(([, , hex]) => hex === iris);
  const swatch = on => h('span', { class: 'sw' }, on ? icon(ICONS.check) : null);
  const tile = (k, label, colour, on, patch, title = label) => h('button', { class: `iris ${on ? 'on' : ''}`, 'aria-pressed': String(on), title,
    dataset: { k: `iris:${k}` }, style: `--c:${colour}`, onclick: () => { if (!on) { put(patch); render(); } } },
  swatch(on), h('span', { class: 'iname' }, label));

  const input = h('input', { type: 'color', value: iris, 'aria-label': 'Any eye colour', dataset: { k: 'iris:custom' } });
  const other = h('label', { class: `iris custom ${custom ? 'on' : ''}`, title: 'Any colour', style: `--c:${iris}` },
    input, swatch(custom), h('span', { class: 'iname' }, 'Other'));
  input.addEventListener('input', () => { other.style.setProperty('--c', input.value); put({ iris_from_photo: false, colors: { iris: input.value } }, false); });
  input.addEventListener('change', () => { put({ iris_from_photo: false, colors: { iris: input.value } }); render(); });

  return h('div', { class: 'eyecol' },
    h('div', { class: 'subhead' }, 'Eye colour',
      h('small', {}, cfg.eyes === false ? 'not showing: Eyes is off under Look' : "the photo's own, or any other")),
    h('div', { class: 'irisgrid', role: 'group', 'aria-label': 'Eye colour' },
      tile('photo', 'Photo', photo.iris || iris, own, { iris_from_photo: true }, "The photo's own eye colour"),
      IRISES.map(([k, label, hex]) => tile(k, label, hex, !own && iris === hex, { iris_from_photo: false, colors: { iris: hex } })),
      other));
}

const pct = v => `${Math.round(v * 100)}%`;
const resetBtn = (keys, label) => h('button', { class: 'linkbtn', title: `Put ${label} back to the defaults`,
  onclick: () => { put(Object.fromEntries(keys.map(k => [k, structuredClone(data.defaults[k])]))); render(); } }, icon(ICONS.reset), 'Reset');

function colourGroup() {
  const c = cfg.colors;
  const presets = Object.entries(data.presets).map(([name, p]) => {
    const on = c.primary === p.primary && c.secondary === p.secondary && c.background === p.background;
    return h('button', { class: `preset ${on ? 'on' : ''}`, 'aria-pressed': String(on), title: name[0].toUpperCase() + name.slice(1),
      dataset: { k: `preset:${name}` }, style: `--pp:${p.primary};--ps:${p.secondary};--pb:${p.background}`,
      onclick: () => { put({ preset: name }); render(); } },
    h('span', { class: 'sw' }, on ? icon(ICONS.check) : null), h('span', { class: 'pname' }, name));
  });
  const well = (key, label) => colorWell(label, c[key], (v, done) => { put({ colors: { [key]: v } }, done); if (done) render(); });
  // the wireframe's glow and rim, the inside of the mouth, the panel (a photo keeps its own skin and lips; the eye
  // colour is under Photo face, and Reset here leaves it be)
  const wells = [well('primary', 'Glow'), well('secondary', 'Rim light'), well('mouth', 'Mouth'), well('background', 'Background')];
  return group({ title: 'Colours', id: 'f-colors', action: h('button', { class: 'linkbtn', title: 'Put the colours back to the defaults',
    onclick: () => { put({ colors: { ...structuredClone(data.defaults.colors), iris: c.iris } }); render(); } }, icon(ICONS.reset), 'Reset') },
  h('div', { class: 'presets' }, presets),
  h('div', { class: 'wells' }, wells),
  h('p', { class: 'privacy' }, "Glow and background also colour this panel and the face's own window."));
}

function lookGroup() {
  const d = data.defaults;
  const s = (key, label, max, help) => slider(label, { value: cfg[key], min: 0, max, step: 0.01, fmt: pct, reset: d[key], k: key, help },
    (v, done) => put({ [key]: v }, done));
  const backgrounds = data.backgrounds.map(id => {
    const on = cfg.background === id;
    return h('button', { class: `bgtile ${on ? 'on' : ''}`, 'aria-pressed': String(on), dataset: { k: `bg:${id}` },
      onclick: () => { if (!on) { put({ background: id }); render(); } } },
    h('span', { class: `bgprev ${id}` }), h('span', {}, id[0].toUpperCase() + id.slice(1)));
  });
  return group({ title: 'Look', id: 'f-look', note: 'Mesh lines, surface, scanlines and points shape the wireframe; they show as the slider moves towards Wireframe.',
    action: resetBtn(['glow', 'wire', 'fill', 'scanlines', 'points', 'edge_fade', 'eyes', 'background'], 'the look') },
    s('glow', 'Glow', 2, 'Light around the wireframe. The skin barely glows.'),
    s('wire', 'Mesh lines', 1),
    s('fill', 'Surface', 1, 'How solid the wireframe looks between its lines.'),
    s('scanlines', 'Scanlines', 1),
    s('points', 'Points', 1, 'Dots on the mesh corners.'),
    s('edge_fade', 'Soft edge', 1, 'Fades the face out towards its edges.'),
    row('Eyes', toggle(cfg.eyes, on => { put({ eyes: on }); render(); }, 'Eyes'), cfg.eyes ? 'Eyeballs with irises.' : 'Off: hollow eyes.'),
    h('div', { class: 'field stack' },
      h('div', { class: 'flabel' }, h('span', { class: 'ftitle' }, 'Panel background'), h('small', {}, 'Behind the face in its floating panel.')),
      h('div', { class: 'bgtiles' }, backgrounds)));
}

// Motion shows eye tracking; the rest (the head's own motion, expressiveness, lip sync) is under Advanced
const ADVANCED_MOTION = ['motion', 'expressiveness', 'mouth_gain', 'sync_offset_ms'];
let motionOpen = (() => { try { return localStorage.getItem('face.motionAdvanced') === '1'; } catch { return false; } })();

function motionGroup() {
  const d = data.defaults;
  const ms = v => (v === 0 ? 'in sync' : `${v > 0 ? '+' : ''}${v} ms`);
  const changed = ADVANCED_MOTION.some(k => cfg[k] !== d[k]);
  const flip = () => {
    motionOpen = !motionOpen;
    try { localStorage.setItem('face.motionAdvanced', motionOpen ? '1' : '0'); } catch { /* not kept: fine */ }
    render();
  };
  return group({ title: 'Motion', id: 'f-motion', action: resetBtn([...ADVANCED_MOTION, 'follow_mouse'], 'motion') },
    row('Eyes follow the mouse', toggle(cfg.follow_mouse !== false, on => { put({ follow_mouse: on }); render(); }, 'Eyes follow the mouse'),
      'The eyes and head turn towards the pointer.'),
    h('button', { class: `advanced ${motionOpen ? 'open' : ''}`, 'aria-expanded': String(motionOpen), dataset: { k: 'motion:advanced' }, onclick: flip },
      h('span', {}, 'Advanced'),
      h('small', {}, motionOpen ? '' : changed ? 'Changed from the defaults' : 'Head motion, expressiveness, lip sync'),
      icon(ICONS.chevron)),
    motionOpen ? [
      slider('Idle motion', { value: cfg.motion, min: 0, max: 2, step: 0.01, fmt: pct, reset: d.motion, k: 'motion', help: 'How much the head moves on its own.' },
        (v, done) => put({ motion: v }, done)),
      slider('Expressiveness', { value: cfg.expressiveness, min: 0, max: 2, step: 0.01, fmt: pct, reset: d.expressiveness, k: 'expressiveness',
        help: 'How strongly it shows expressions and gestures.' }, (v, done) => put({ expressiveness: v }, done)),
      slider('Lip-sync strength', { value: cfg.mouth_gain, min: 0.3, max: 2, step: 0.01, fmt: pct, reset: d.mouth_gain, k: 'mouth_gain',
        help: 'How wide the mouth opens as it talks.' }, (v, done) => put({ mouth_gain: v }, done)),
      slider('Lip-sync timing', { value: cfg.sync_offset_ms, min: -200, max: 200, step: 5, fmt: ms, reset: d.sync_offset_ms, k: 'sync_offset_ms',
        help: 'If the mouth runs ahead of the voice, move it later (+); if it trails, earlier.' }, (v, done) => put({ sync_offset_ms: v }, done)),
    ] : null);
}

function expressionGroup() {
  const play = name => {
    send('/api/face/expression', 'POST', { name, seconds: 4 }).catch(e => toast(e.message));
    for (const t of playing.values()) clearTimeout(t);
    playing.clear();
    if (name !== 'neutral') playing.set(name, setTimeout(() => { playing.delete(name); render(); }, 4000));
    render();
  };
  return group({ title: 'Expressions', id: 'f-expr', note: 'Try one on the face for a few seconds. The agent changes its expression with its face tool too.' },
    h('div', { class: 'exprs' }, data.expressions.map(name => h('button', {
      class: `expr ${playing.has(name) ? 'on' : ''}`, dataset: { k: `expr:${name}` }, onclick: () => play(name) },
    glyph(name), h('span', {}, name[0].toUpperCase() + name.slice(1))))));
}

function windowGroup() {
  const floating = cfg.mode === 'floating', windowed = !SHELL || !floating;
  return group({ title: 'Window', id: 'f-window' },
    SHELL ? row('View', seg([['floating', 'Floating face'], ['windowed', 'Floating panel']], cfg.mode,
      v => { put({ mode: v }); render(); }, { label: 'View' }),
    floating ? 'Just the face, on the desktop. Scroll over it to resize it.'
      : 'The face in a small window with captions and buttons. Scroll over the face to zoom; Ctrl+scroll resizes the window.',
    { class: 'wide' }) : null,
    windowed ? row('Captions', toggle(!!cfg.captions, on => { put({ captions: on }); render(); }, 'Captions'), 'Subtitles under the face in its panel.') : null,
    SHELL ? row('Always on top', toggle(!!cfg.on_top, on => { put({ on_top: on }); render(); }, 'Always on top'), 'Keep the face above other windows.') : null);
}

// --- jumping between groups ------------------------------------------------------------------------------------------
const SECTIONS = [['skin', 'Skin', 'f-skin'], ['photo', 'Photo', 'f-photo'], ['colors', 'Colours', 'f-colors'], ['look', 'Look', 'f-look'],
  ['motion', 'Motion', 'f-motion'], ['expressions', 'Expressions', 'f-expr'], ['window', 'Window', 'f-window']];
let current = 'skin';

function jump(name) {
  const s = SECTIONS.find(x => x[0] === name || x[2] === name);
  const el = s && $(s[2]);
  if (!el) return;
  root().scrollTo({ top: el.offsetTop - 52, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1400);
}

let spyRaf = 0;
function spy() {
  if (spyRaf) return;
  spyRaf = requestAnimationFrame(() => {
    spyRaf = 0;
    const top = root().scrollTop + 70;
    let on = SECTIONS[0][0];
    for (const [name, , id] of SECTIONS) { const el = $(id); if (el && el.offsetTop <= top) on = name; }
    if (root().scrollTop + root().clientHeight >= root().scrollHeight - 4) on = SECTIONS.at(-1)[0];
    if (on !== current) { current = on; markNav(); }
  });
}
function markNav() {
  const nav = root().querySelector('.subnav');
  if (!nav) return;
  for (const b of nav.children) b.classList.toggle('on', b.dataset.go === current);
  const b = nav.querySelector('.on');
  if (b) nav.scrollTo({ left: b.offsetLeft - (nav.clientWidth - b.offsetWidth) / 2, behavior: 'smooth' });
}

// --- drawing ------------------------------------------------------------------------------------------------------------
function render() {
  if (!cfg || !data) return;
  keepFocus(root(), () => root().replaceChildren(
    h('nav', { class: 'subnav', 'aria-label': 'Face settings' },
      SECTIONS.map(([name, label]) => h('button', { class: name === current ? 'on' : '', dataset: { go: name }, onclick: () => jump(name) }, label))),
    h('div', { class: 'page' }, skinGroup(), photoGroup(), colourGroup(), lookGroup(), motionGroup(), expressionGroup(), windowGroup())));
}
