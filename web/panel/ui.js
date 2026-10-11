// Controls shared by the Face, Voice and Agent tabs: groups, rows, switches, selects, segmented choices, sliders and
// colour wells. Each takes its current value and an onChange callback; the caller saves and redraws.
import { h } from './common.js';

const NS = 'http://www.w3.org/2000/svg';

/** An SVG element (for icons and the little drawings), attributes set as given. */
export function svg(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...kids);
  return el;
}

/** A 24x24 stroked icon: path data, several paths separated by '|'. */
export const icon = d => svg('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' }, ...d.split('|').map(p => svg('path', { d: p })));

export const ICONS = {
  check: 'M5 12.5l4.5 4.5L19 7.5',
  upload: 'M12 15.5V4.5M7.5 9L12 4.5 16.5 9|M5 15v3.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V15',
  trash: 'M5 7h14M10 7V4.5h4V7M7 7l1 12.5h8L17 7',
  reset: 'M4.5 5v5h5|M5.2 14.5A7.5 7.5 0 1 0 6.6 7.2L4.5 10',
  refresh: 'M19.5 5v5h-5|M18.8 14.5A7.5 7.5 0 1 1 17.4 7.2l2.1 2.8',
  folder: 'M3.5 7.5A2 2 0 0 1 5.5 5.5h3.6l2 2h7.4a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z',
  copy: 'M9 9h10v10H9z|M15 9V5H5v10h4',
  image: 'M4 5.5h16v13H4z|M4 15l4.5-4.5 4 4 2.5-2.5 5 5|M15.5 9.2v.1',
  person: 'M12 4.5a3.8 3.8 0 1 0 0 7.6a3.8 3.8 0 0 0 0-7.6z|M4.5 20c.9-3.6 3.9-5.6 7.5-5.6s6.6 2 7.5 5.6',
  play: 'M8 5.5v13l10.5-6.5z',
  shield: 'M12 3.5l7.5 2.8v5.6c0 4.6-3.2 7.7-7.5 8.6-4.3-.9-7.5-4-7.5-8.6V6.3z|M12 9v4|M12 16v.1',
  info: 'M12 3.5a8.5 8.5 0 1 0 0 17a8.5 8.5 0 0 0 0-17z|M12 11v5|M12 7.8v.1',
  alert: 'M12 4l9 15.5H3z|M12 10v4.5|M12 17v.1',
  speaker: 'M4 9v6h4l5 4V5L8 9z|M16 9a4 4 0 0 1 0 6|M18.5 6.5a8 8 0 0 1 0 11',
  key: 'M14.5 4.5a5 5 0 1 0 0 10a5 5 0 0 0 0-10z|M11 13l-6.5 6.5|M7 17l2 2',
  chevron: 'M6.5 9.5l5.5 5.5 5.5-5.5',
};

/** A group of settings: a small heading (with an optional action on the right) over a card of rows. */
export function group({ title, id, note, action }, ...rows) {
  return h('section', { class: 'sgroup', id },
    h('header', { class: 'sgroup-head' }, h('h2', {}, title), action || null),
    note ? h('p', { class: 'sgroup-note' }, note) : null,
    h('div', { class: 'card' }, ...rows));
}

/** A settings row: label and help on the left, the control on the right (below it, for wide controls on a narrow panel). */
export function row(label, control, help, extra = {}) {
  const { class: cls = '', ...attrs } = extra;
  return h('div', { class: `field ${cls}`, ...attrs },
    h('div', { class: 'flabel' }, h('span', { class: 'ftitle' }, label), help ? h('small', {}, help) : null),
    h('div', { class: 'fctl' }, control));
}

export function toggle(checked, onChange, label, disabled = false) {
  return h('button', { class: `switch ${checked ? 'on' : ''}`, role: 'switch', 'aria-checked': String(checked), 'aria-label': label,
    disabled: disabled || null, dataset: { k: `sw:${label}` }, onclick: () => onChange(!checked) }, h('span', { class: 'knob' }));
}

export function select(options, value, onChange, disabled = false) {
  const el = h('select', { disabled: disabled || null, onchange: () => onChange(el.value) },
    options.map(([v, label]) => h('option', { value: v, selected: v === value || null }, label)));
  return noWheel(el);
}

/** Scrolling the panel with the mouse wheel must not change a dropdown under the pointer (Chromium on Windows steps a
 *  focused <select> through its options, which once switched the agent without anyone choosing): scroll instead. */
export function noWheel(el) {
  el.addEventListener('wheel', e => {
    e.preventDefault();
    if (document.activeElement === el) el.blur();
    let p = el.parentElement;
    while (p && !(p.scrollHeight > p.clientHeight && /(auto|scroll)/.test(getComputedStyle(p).overflowY))) p = p.parentElement;
    (p || document.scrollingElement)?.scrollBy({ top: e.deltaY });
  }, { passive: false });
  return el;
}

export function text(value, onChange, { placeholder = '', readonly = false } = {}) {
  const el = h('input', { type: 'text', value: value ?? '', placeholder, readonly: readonly || null, spellcheck: 'false',
    onchange: () => onChange(el.value.trim()) });
  return el;
}

/** One of a few choices, side by side: options [[value, label, title?]]. */
export function seg(options, value, onChange, { label = '', cls = '' } = {}) {
  return h('div', { class: `seg ${cls}`, role: 'group', 'aria-label': label },
    options.map(([v, text, title]) => h('button', { class: v === value ? 'on' : '', 'aria-pressed': String(v === value), title,
      dataset: { k: `seg:${label}:${v}` }, onclick: () => { if (v !== value) onChange(v); } }, text)));
}

/**
 * A labelled slider with its value shown. onChange(value, done): done is false while dragging, true when let go.
 * Double-click puts it back to `reset`.
 */
export function slider(label, { value, min, max, step = 0.01, fmt = v => v.toFixed(2), reset, k, help } = {}, onChange) {
  const out = h('output', { class: 'sval' });
  const input = h('input', { type: 'range', min, max, step, value, 'aria-label': label, dataset: { k: `sl:${k || label}` } });
  const paint = () => {
    input.style.setProperty('--pct', `${((Number(input.value) - min) / (max - min)) * 100}%`);
    out.textContent = fmt(Number(input.value));
  };
  paint();
  input.addEventListener('input', () => { paint(); onChange(Number(input.value), false); });
  input.addEventListener('change', () => onChange(Number(input.value), true));
  if (reset !== undefined) {
    input.addEventListener('dblclick', () => { input.value = reset; paint(); onChange(reset, true); });
    input.title = 'Double-click to reset';
  }
  return h('div', { class: 'field slider' },
    h('div', { class: 'srow' }, h('div', { class: 'flabel' }, h('span', { class: 'ftitle' }, label), help ? h('small', {}, help) : null), out),
    input);
}

/** A colour well: a swatch with its name and value, opening the system picker. onChange(hex, done) like a slider. */
export function colorWell(label, value, onChange) {
  const input = h('input', { type: 'color', value, 'aria-label': label, dataset: { k: `col:${label}` } });
  const code = h('code', {}, value);
  const well = h('label', { class: 'well', style: `--c:${value}` }, input, h('span', { class: 'dot' }),
    h('span', { class: 'wname' }, label), code);
  input.addEventListener('input', () => { well.style.setProperty('--c', input.value); code.textContent = input.value; onChange(input.value, false); });
  input.addEventListener('change', () => onChange(input.value, true));
  return well;
}

export function btn(label, onClick, { kind = '', ico = '', title, disabled = false } = {}) {
  return h('button', { class: `btn ${kind}`, title, disabled: disabled || null, onclick: onClick }, ico ? icon(ico) : null, label);
}

/** A status line in a card: ok / bad / busy / plain. */
export const note = (kind, ...kids) => h('div', { class: `callout ${kind}` },
  icon(kind === 'bad' ? ICONS.alert : kind === 'ok' ? ICONS.check : ICONS.info), h('div', {}, ...kids));

/** Put focus back on the control it was on before a redraw (controls carry a data-k key). */
export function keepFocus(root, draw) {
  const k = root.contains(document.activeElement) ? document.activeElement.dataset?.k : null;
  draw();
  if (k) root.querySelector(`[data-k="${CSS.escape(k)}"]`)?.focus({ preventScroll: true });
}
