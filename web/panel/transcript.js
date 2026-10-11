// The Activity tab's Raw view (offered while the agent is Claude Code): Claude Code's own session file, followed live.
// Far more than the summary: every prompt, reply and thought, and each tool call with its complete input and output
// (results sit under their call).
// Entries come from /api/transcript (the recent backlog) and the WebSocket ("transcript": new entries; "transcript_reset":
// another session, so start again). Everything here is the agent's text: put in the page as text only, never HTML.
import { $, h, send, fmtMs, fmtTime, fmtCost } from './common.js';
import { icon, ICONS } from './ui.js';

const MAX_ROWS = 600;                 // drawn entries kept; older ones fall off the top
const TEXT_LIMIT = 2400, OUT_LIMIT = 1800, FIELD_LIMIT = 1200;
let rows = [];                        // [{ id, e, el, tool? }] oldest first, as drawn
const tools = new Map();              // tool_use_id -> { use, result, el }
const opened = new Set(), full = new Set();
let info = { available: false };      // the session followed: { available, session_id, path, exists }
let backend = null;                   // { type, label } from the status
let follow = true, unseen = 0, contexts = 0, quiet = true, title = '', cost = null, showContext = false;
let onFresh = () => {}, summary = () => {};

const log = () => $('tlog');
const toolKey = e => e.tool_use_id || e.id;

// --- feeding it -------------------------------------------------------------------------------------------------
/** hooks: onFresh(n) for entries that arrive, summary() to go back to the Activity summary. */
export function init(hooks) {
  ({ onFresh, summary } = hooks);
  log().addEventListener('scroll', () => {
    follow = log().scrollHeight - log().scrollTop - log().clientHeight < 40;
    if (follow) unseen = 0;
    showJump();
  });
  $('tjump').addEventListener('click', () => { follow = true; unseen = 0; log().scrollTop = log().scrollHeight; showJump(); });
  toolbar();
}

export async function load() {
  const view = await send('/api/transcript?limit=400');
  clear(view);
  quiet = true;
  add(view.entries || []);
  quiet = false;
  drawState();
  log().scrollTop = log().scrollHeight;
}

/** New entries, live. */
export function append(entries) {
  if (!info.available) { load().catch(() => {}); return; }
  const fresh = add(entries);
  if (fresh > 0) onFresh(fresh);
  if (!follow) unseen += fresh;
  showJump();
}

/** The session changed (new conversation, another folder or agent): start again. */
export function reset(view) {
  clear(view);
  drawState();
  load().catch(() => {});
}

export function setStatus(s) {
  const b = s.backend || null;
  if (JSON.stringify(b) === JSON.stringify(backend)) return;
  backend = b;
  drawState();
}

export function shown() {
  if (follow) log().scrollTop = log().scrollHeight;
}

function clear(view) {
  info = { available: !!view.available, session_id: view.session_id, path: view.path, exists: !!view.exists };
  rows = [];
  tools.clear();
  contexts = 0;
  title = '';
  cost = null;
  unseen = 0;
  follow = true;
  log().replaceChildren();
  showJump();
}

/** Draw entries at the end; returns how many rows can be seen of them (context notes are hidden unless asked for). */
function add(entries) {
  const el = log();
  const atEnd = follow;
  let seen = 0;
  for (const e of entries) {
    if (e.kind === 'title') { title = e.text; continue; }
    if (e.kind === 'cost') { cost = Number(e.text); continue; }
    if (e.kind === 'tool_result' && tools.has(e.tool_use_id)) {
      const t = tools.get(e.tool_use_id);
      t.result = e;
      redraw(t);
      continue;
    }
    const node = build(e);
    if (!node) continue;
    if (!quiet) node.classList.add('enter');
    el.append(node);
    rows.push({ id: e.id, e, el: node, tool: e.kind === 'tool_use' ? toolKey(e) : null });
    if (showContext || e.kind !== 'context') seen++;
  }
  // keep the page light: the oldest rows go (without moving what is being read)
  if (rows.length > MAX_ROWS) {
    const drop = rows.splice(0, rows.length - MAX_ROWS);
    const lost = drop.reduce((sum, r) => sum + r.el.offsetHeight, 0);
    for (const r of drop) { r.el.remove(); if (r.tool) tools.delete(r.tool); if (r.e.kind === 'context') contexts--; }
    if (!atEnd) el.scrollTop -= lost;
  }
  drawToolbar();
  if (rows.length) el.querySelector(':scope > .tempty')?.remove();
  if (atEnd) el.scrollTop = el.scrollHeight;
  return seen;
}

// --- one entry ------------------------------------------------------------------------------------------------------
function build(e) {
  const side = e.sidechain ? ' side' : '';
  switch (e.kind) {
    case 'prompt':
      return h('div', { class: `trow prompt${side}` },
        h('div', { class: 'tmeta' }, h('span', { class: 'who' }, e.sidechain ? 'Task for a subagent' : 'You'), h('span', { class: 'at' }, fmtTime(e.ts))),
        clip(e.text, TEXT_LIMIT, e.id, 'ttext'));
    case 'text':
      return h('div', { class: `trow say${side}` }, e.sidechain ? h('span', { class: 'tag' }, 'Subagent') : null, clip(e.text, TEXT_LIMIT, e.id, 'ttext'));
    case 'thinking':
      return thinking(e, side);
    case 'tool_use': {
      const t = { use: e, result: null, el: null };
      tools.set(toolKey(e), t);
      t.el = toolEl(t);
      return t.el;
    }
    case 'tool_result':                                          // its call fell off the backlog: the result alone
      return h('div', { class: `trow orphan${side}` }, h('div', { class: 'tmeta' }, h('span', { class: 'who' }, 'Result')),
        clip(e.text || '(empty)', OUT_LIMIT, e.id, `tout ${e.is_error ? 'bad' : ''}`));
    case 'context': {
      contexts++;
      const open = opened.has(e.id);
      return h('div', { class: `trow ctx${side}` },
        h('button', { class: 'thead', 'aria-expanded': String(open), onclick: () => toggle(e.id) },
          icon(ICONS.info), h('span', { class: 'tname' }, `Context: ${(e.name || 'note').replace(/_/g, ' ')}`),
          open ? null : h('span', { class: 'tbrief' }, firstLine(e.text))),
        open ? clip(e.text, OUT_LIMIT, e.id, 'tout') : null);
    }
    default:
      return null;
  }
}

function thinking(e, side) {
  const secs = e.duration_ms ? ` for ${fmtMs(e.duration_ms)}` : '';
  if (!e.text) return h('div', { class: `trow think${side}` }, h('span', { class: 'tthought' }, `Thought${secs}`));
  const open = opened.has(e.id);
  return h('div', { class: `trow think${side}` },
    h('button', { class: 'thead', 'aria-expanded': String(open), onclick: () => toggle(e.id) },
      h('span', { class: 'tthought' }, `Thinking${secs}`), open ? null : h('span', { class: 'tbrief' }, firstLine(e.text))),
    open ? clip(e.text, TEXT_LIMIT, e.id, 'ttext soft') : null);
}

/** A tool call: its name and what it was run on; open, its whole input and output. */
function toolEl(t) {
  const { use, result } = t;
  const open = opened.has(use.id);
  const state = !result ? 'running' : result.is_error ? 'error' : 'ok';
  const outline = result ? (result.is_error ? firstLine(result.text) : lines(result.text)) : 'no result yet';
  const el = h('div', { class: `trow tool ${state}${use.sidechain ? ' side' : ''}` },
    h('button', { class: 'thead', 'aria-expanded': String(open), onclick: () => toggle(use.id) },
      h('span', { class: `sdot ${state}`, role: 'img', 'aria-label': state }),
      h('span', { class: 'chip' }, use.name || 'tool'),
      h('span', { class: 'tbrief', title: brief(use.name, use.input) }, brief(use.name, use.input)),
      h('span', { class: `tres ${state}` }, outline)),
    open ? h('div', { class: 'tdetail' },
      fields(use.input, use.id),
      result ? h('div', { class: 'tlabel' }, result.is_error ? 'Error' : 'Output') : null,
      result ? clip(result.text || '(empty)', OUT_LIMIT, `${use.id}:out`, `tout ${result.is_error ? 'bad' : ''}`) : null) : null);
  return el;
}

function redraw(t) {
  const fresh = toolEl(t);
  t.el.replaceWith(fresh);
  const r = rows.find(x => x.el === t.el);
  if (r) r.el = fresh;
  t.el = fresh;
}

function toggle(id) {
  if (opened.has(id)) opened.delete(id); else opened.add(id);
  const r = rows.find(x => x.id === id);
  if (!r) return;
  if (r.tool) { redraw(tools.get(r.tool)); return; }
  if (r.e.kind === 'context') contexts--;                        // build() counts it again
  const fresh = build(r.e);
  r.el.replaceWith(fresh);
  r.el = fresh;
}

// --- bits ------------------------------------------------------------------------------------------------------------
const firstLine = t => (t || '').trim().split('\n')[0].slice(0, 200);
const lines = t => {
  const n = (t || '').replace(/\n+$/, '').split('\n').length;
  return !t ? 'no output' : n === 1 ? firstLine(t) : `${n} lines`;
};

/** What a tool call was run on, in a line: the command, the file, the pattern, the address... */
function brief(name, input) {
  let o;
  try { o = JSON.parse(input || '{}'); } catch { return firstLine(input); }
  if (!o || typeof o !== 'object') return String(o);
  const v = o.command ?? o.file_path ?? o.notebook_path ?? o.path ?? o.pattern ?? o.url ?? o.query ?? o.description ?? o.prompt ?? o.skill;
  if (v !== undefined) return firstLine(String(v));
  if (Array.isArray(o.todos)) return `${o.todos.length} to-dos`;
  const s = Object.values(o).find(x => typeof x === 'string');
  return s ? firstLine(s) : '';
}

/** A tool's input as name / value pairs (texts as they are, not JSON-escaped). */
function fields(input, id) {
  let o;
  try { o = JSON.parse(input || ''); } catch { o = null; }
  if (!o || typeof o !== 'object' || Array.isArray(o)) return input ? clip(input, FIELD_LIMIT, `${id}:in`, 'tout') : null;
  return h('div', { class: 'tfields' }, Object.entries(o).map(([k, v]) => h('div', { class: 'tfield' },
    h('span', { class: 'tkey' }, k),
    clip(typeof v === 'string' ? v : JSON.stringify(v, null, 2), FIELD_LIMIT, `${id}:in:${k}`, 'tval'))));
}

/** Long text, cut to `limit` characters with a "Show all" (remembered by key). */
function clip(text, limit, key, cls) {
  const t = text || '';
  const cut = t.length > limit && !full.has(key);
  const box = h('div', { class: cls }, cut ? t.slice(0, limit) : t);
  if (!cut) return box;
  return h('div', { class: 'tclip' }, box, h('button', { class: 'more', onclick: e => {
    full.add(key);
    const wrap = e.currentTarget.parentElement;
    wrap.replaceWith(h('div', { class: cls }, t));
  } }, `Show all (${(t.length - limit).toLocaleString()} more characters)`));
}

// --- the toolbar and the states around the log -----------------------------------------------------------------------
function toolbar() {
  $('ttools').replaceChildren(
    h('span', { class: 'tlive', id: 'tlive' }, h('span', { class: 'vdot' }), h('span', { id: 'tlive-text' }, 'Off')),
    h('span', { class: 'ttitle', id: 'ttitle' }),
    h('span', { class: 'schip cost', id: 'tcost', hidden: true }),
    h('button', { class: 'chipbtn', id: 'tctx', 'aria-pressed': 'false', title: 'Show what Claude Code adds to the conversation itself (environment, reminders)',
      onclick: () => {
        showContext = !showContext;
        log().classList.toggle('show-context', showContext);
        drawToolbar();
      } }, 'Context'),
    h('button', { class: 'iconbtn', id: 'tcopy', title: 'Copy the transcript file\'s path', 'aria-label': 'Copy the transcript file\'s path',
      onclick: () => info.path && navigator.clipboard?.writeText(info.path).then(() => $('tcopy').classList.add('done'),
        () => {}).then(() => setTimeout(() => $('tcopy').classList.remove('done'), 1200)) }, icon(ICONS.copy)));
}

function drawToolbar() {
  const live = info.available && info.exists;
  $('tlive').dataset.state = live ? 'live' : info.available ? 'waiting' : 'off';
  $('tlive-text').textContent = live ? 'Live' : info.available ? 'Waiting' : 'Off';
  $('ttitle').textContent = title;
  $('ttitle').title = title;
  $('tcost').hidden = cost === null || Number.isNaN(cost);
  $('tcost').textContent = cost === null ? '' : fmtCost(cost);
  $('tctx').textContent = contexts ? `Context ${contexts}` : 'Context';
  $('tctx').setAttribute('aria-pressed', String(showContext));
  $('tctx').classList.toggle('on', showContext);
  $('tctx').hidden = !contexts;
  $('tcopy').hidden = !info.path;
  $('tcopy').title = info.path ? `Copy the transcript file's path:\n${info.path}` : '';
}

function drawState() {
  drawToolbar();
  const el = log();
  el.querySelector(':scope > .tempty')?.remove();
  if (rows.length) return;
  const claude = !backend || backend.type === 'claude';
  let body;
  if (!info.available && !claude) {
    body = [icon(ICONS.info), h('strong', {}, `${backend.label} keeps its own transcript`),
      h('span', {}, 'The raw transcript is Claude Code\'s session file. What this agent does shows in the summary.'),
      h('button', { class: 'btn sm', onclick: () => summary() }, 'Show the summary')];
  } else if (!info.available) {
    body = [icon('M3 12h4l3-7 4 14 3-7h4'), h('strong', {}, 'No session yet'),
      h('span', {}, 'The transcript starts with the first message, and follows the session live from then on.')];
  } else {
    body = [h('span', { class: 'spin' }), h('strong', {}, info.exists ? 'Nothing in this session yet' : 'Waiting for the session file'),
      info.path ? h('code', { title: info.path }, info.path) : null];
  }
  el.append(h('div', { class: 'empty tempty' }, body));
}

function showJump() {
  const b = $('tjump');
  b.hidden = follow;
  b.replaceChildren(icon('M12 5v13M6.5 12.5L12 18l5.5-5.5'), unseen ? `Jump to latest (${unseen} new)` : 'Jump to latest');
}
