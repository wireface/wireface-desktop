// The activity tab: the agent's session as a readable timeline.
//   turn (what you asked)  ->  what it said, and what it did (tool calls, grouped; subagents and background
//   tasks nested under the call that started them)  ->  a footer with the time and cost.
// With the realtime voice, a reply it read out word for word is marked "spoken" (no second copy); where what it said
// differs (cut off, a path or link described, a long reply summed up) its words are shown under the reply. What the
// voice said in its own words ("on it", progress, answers while the agent was busy, questions) is a "Voice" line.
// Events arrive from /api/activity (backlog) and the WebSocket; an event with a known id replaces its old self.
// For Claude Code the tab has a second view, Raw: Claude Code's own transcript of the session (transcript.js), with
// every tool's complete output; a result cut short here can also be fetched whole from there ("Show full output").
import { $, h, send, fmtMs, fmtTime, fmtCost, fmtTokens } from './common.js';
import { btn, icon, ICONS } from './ui.js';
import * as transcript from './transcript.js';

const events = new Map();                 // id -> event
const byTurn = new Map();                 // turn id -> [event ids]
const blocks = new Map();                 // render key -> element
const dirty = new Set();
const opened = new Set(), closed = new Set(), fullText = new Set();    // what the user expanded / collapsed
const spokenAs = new Map();               // agent reply id -> the voice event that read it out
const readsOut = new Map();               // voice event id -> the reply ids it read out
let filter = 'all', query = '', follow = true, clearedBefore = 0, raf = 0, openTab = () => {};
// rows seen before animate no more: only what arrives while the panel is open slides in
const seen = new Set();
let quiet = true;

const CATEGORY = { assistant: 'conversation', thinking: 'conversation', notice: 'conversation', turn: 'conversation',
  tool: 'tools', permission: 'tools', task: 'background', error: 'errors', voice: 'conversation', heard: 'conversation' };

// --- feeding it -------------------------------------------------------------------------------------------------
export async function load() {
  const { events: list } = await send('/api/activity?limit=1500');
  for (const ev of list) store(ev);
  for (const key of blocks.keys()) dirty.add(key);
  quiet = true;
  schedule();
}

export function upsert(ev) {
  store(ev);
  schedule();
}

function keyOf(ev) {
  if (ev.kind === 'turn') return `turn:${ev.id}`;
  return ev.turn_id ? `turn:${ev.turn_id}` : `ev:${ev.id}`;
}

function store(ev) {
  const fresh = !events.has(ev.id);
  events.set(ev.id, ev);
  if (fresh && ev.turn_id) {
    if (!byTurn.has(ev.turn_id)) byTurn.set(ev.turn_id, []);
    byTurn.get(ev.turn_id).push(ev.id);
  }
  dirty.add(keyOf(ev));
  if (ev.kind === 'voice' && ev.name === 'face') linkVoice(ev);
}

// --- which reply a voice line read out ----------------------------------------------------------------------------
const wordsOf = t => (t || '').toLowerCase().replace(/[-_]/g, ' ').match(/[a-z0-9']+/g) || [];

/** Words in common, in order, as a share of both (1: the same words). */
function sameWords(a, b) {
  const x = wordsOf(a).slice(0, 200), y = wordsOf(b).slice(0, 200);
  if (!x.length || !y.length) return 0;
  let prev = new Array(y.length + 1).fill(0);
  for (const w of x) {
    const row = [0];
    for (let j = 0; j < y.length; j++) row.push(w === y[j] ? prev[j] + 1 : Math.max(prev[j + 1], row[j]));
    prev = row;
  }
  return (2 * prev[y.length]) / (x.length + y.length);
}

function linkVoice(ev) {
  let ids = ev.extra?.relays;
  let same = ev.extra?.same;
  if (!ids) {
    // logged before voice lines said what they read out: the reply just before it with mostly the same words
    let best = null, score = 0;
    for (const a of events.values()) {
      if (a.kind !== 'assistant' || a.parent_id || a.ts > ev.ts || ev.ts - a.ts > 300) continue;
      const s = sameWords(a.output, ev.output);
      if (s > score) { best = a; score = s; }
    }
    if (!best || score < 0.6) return;                            // its own words, not a reply
    ids = [best.id];
    same = score >= 0.9;
  }
  readsOut.set(ev.id, { ids, same: !!same && !ev.extra?.interrupted, kind: ev.extra?.kind || 'relay' });
  for (const id of ids) {
    const prev = spokenAs.get(id);
    if (!prev || prev.ts <= ev.ts) spokenAs.set(id, ev);
    const a = events.get(id);
    if (a) dirty.add(keyOf(a));
  }
}

/** A voice line that read out a reply shown here is drawn with that reply, not on its own. */
const drawnWithReply = ev => ev.kind === 'voice' && readsOut.has(ev.id) && readsOut.get(ev.id).ids.some(id => events.has(id));
/** What the judge passed on to the agent shows as the agent's turn: only what it kept back or acted on is drawn. */
const passedOn = ev => ev.kind === 'heard' && ev.extra?.action === 'send';

function schedule() {
  if (!raf) raf = requestAnimationFrame(render);
}

// --- what is shown ----------------------------------------------------------------------------------------------
const textOf = ev => `${ev.name} ${ev.summary} ${ev.input || ''} ${ev.output || ''}`.toLowerCase();
const matches = ev => !query || textOf(ev).includes(query);

function passes(ev) {
  if (ev.ts < clearedBefore) return false;
  if (!matches(ev)) return false;
  if (filter === 'all') return true;
  if (filter === 'errors') return ev.status === 'error' || ev.kind === 'error';
  return CATEGORY[ev.kind] === filter;
}

function stepsOf(turnId) {
  return (byTurn.get(turnId) || []).map(id => events.get(id)).filter(Boolean).sort((a, b) => a.ts - b.ts);
}

// --- building the DOM -------------------------------------------------------------------------------------------
function render() {
  raf = 0;
  const log = $('log');
  for (const key of dirty) {
    const el = key.startsWith('turn:') ? buildTurn(key.slice(5)) : buildStandalone(key.slice(3));
    const old = blocks.get(key);
    if (!el) {
      if (old) { old.remove(); blocks.delete(key); }
      continue;
    }
    if (old) old.replaceWith(el);
    else {
      const ts = Number(el.dataset.ts);
      const after = [...log.children].find(c => c.dataset.ts && Number(c.dataset.ts) > ts);
      log.insertBefore(el, after || null);
    }
    blocks.set(key, el);
  }
  dirty.clear();
  quiet = false;
  let empty = log.querySelector('.empty');
  if (!log.querySelector('.turn, .loose')) {
    const filtered = query || filter !== 'all';
    if (!empty) log.append(h('div', { class: 'empty' }, icon(filtered ? ICONS.info : 'M3 12h4l3-7 4 14 3-7h4'),
      h('strong', {}, filtered ? 'Nothing matches' : 'Nothing yet'),
      h('span', {}, filtered ? 'Try another filter or search.' : 'Ask the face for something and its work shows up here.')));
  } else if (empty) empty.remove();
  if (follow) log.scrollTop = log.scrollHeight;
  $('jump').hidden = follow;
}

function rebuildAll() {
  quiet = true;
  for (const key of blocks.keys()) dirty.add(key);
  for (const ev of events.values()) dirty.add(keyOf(ev));
  schedule();
}

function buildStandalone(id) {
  const ev = events.get(id);
  if (!ev || !passes(ev) || drawnWithReply(ev) || passedOn(ev)) return null;
  const wrap = h('div', { class: `loose ${fresh(`loose:${id}`)}`, dataset: { ts: ev.ts } });
  wrap.append(...renderNodes([{ ev, kids: [] }], 0));
  return wrap;
}

function buildTurn(turnId) {
  const turn = events.get(turnId);
  const steps = stepsOf(turnId).filter(ev => !drawnWithReply(ev));
  const inTurn = new Set(steps.map(s => s.id));
  const kids = new Map();
  const roots = [];
  for (const ev of steps) {
    const node = { ev, kids: [] };
    kids.set(ev.id, node);
  }
  for (const ev of steps) {
    const node = kids.get(ev.id);
    if (ev.parent_id && inTurn.has(ev.parent_id)) kids.get(ev.parent_id).kids.push(node);
    else roots.push(node);
  }
  const visibleNodes = pruneTree(roots);
  const promptShown = turn && (filter === 'all' || filter === 'conversation') && turn.ts >= clearedBefore
    && (!query || (turn.input || turn.summary || '').toLowerCase().includes(query));
  const errorTurn = filter === 'errors' && turn && turn.status === 'error';
  if (!visibleNodes.length && !promptShown && !errorTurn) return null;

  const ts = turn ? turn.ts : (steps[0]?.ts ?? Date.now() / 1000);
  const block = h('section', { class: `turn ${turn ? turn.status : 'ok'} ${fresh(`turn:${turnId}`)}`, dataset: { ts } });
  if (turn) block.append(turnHead(turn));
  else block.append(h('div', { class: 'turn-head earlier' }, h('span', { class: 'when' }, 'Earlier activity')));
  const body = h('div', { class: 'steps' });
  body.append(...renderNodes(visibleNodes, 0));
  block.append(body);
  const foot = turn && turn.status !== 'running' ? turnFoot(turn) : null;
  if (foot) block.append(foot);
  return block;
}

/** The nodes that pass the filter, keeping a parent when any of its children pass. */
function pruneTree(nodes) {
  const out = [];
  for (const n of nodes) {
    n.kids = pruneTree(n.kids);
    if (passes(n.ev) || n.kids.length) out.push(n);
  }
  return out;
}

function turnHead(turn) {
  const background = turn.extra?.source === 'background';
  return h('div', { class: 'turn-head' },
    h('div', { class: `prompt ${background ? 'bg' : ''}` }, background ? 'Background activity' : (turn.input || turn.summary || '(no text)')),
    h('span', { class: 'when' }, fmtTime(turn.ts), turn.status === 'running' ? h('span', { class: 'spin', 'aria-label': 'working' }) : null));
}

function turnFoot(turn) {
  const x = turn.extra || {};
  // before session_cost_usd was recorded, Claude Code's cost_usd was the session's running total
  const legacy = x.turns !== undefined && !('session_cost_usd' in x);
  const cost = x.cost_usd == null ? '' : legacy ? `${fmtCost(x.cost_usd)} session total` : fmtCost(x.cost_usd);
  const bits = [fmtMs(turn.duration_ms), x.tokens ? `${fmtTokens(x.tokens)} tokens` : '', cost,
    x.turns > 1 ? `${x.turns} steps` : '', x.interrupted ? 'stopped by you' : '', turn.status === 'error' && !x.interrupted ? 'failed' : '']
    .filter(Boolean);
  return bits.length ? h('div', { class: 'turn-foot' }, bits.map(b => h('span', {}, b))) : null;
}

/** Consecutive tool calls become one collapsible group; everything else is a row of its own. */
function renderNodes(nodes, depth) {
  const out = [];
  for (let i = 0; i < nodes.length;) {
    const toolish = n => n.ev.kind === 'tool' || n.ev.kind === 'permission';
    if (toolish(nodes[i])) {
      let j = i;
      while (j < nodes.length && toolish(nodes[j])) j++;
      const run = nodes.slice(i, j);
      out.push(run.length > 1 ? toolGroup(run, depth) : stepRow(run[0], depth));
      i = j;
    } else {
      out.push(stepRow(nodes[i], depth));
      i++;
    }
  }
  return out;
}

function toolGroup(run, depth) {
  const id = `group:${run[0].ev.id}`;
  const running = run.some(n => n.ev.status === 'running');
  const failed = run.filter(n => n.ev.status === 'error').length;
  const open = isOpen(id, running || !!query || filter === 'tools' || filter === 'errors');
  const counts = {};
  for (const n of run) counts[n.ev.name || 'tool'] = (counts[n.ev.name || 'tool'] || 0) + 1;
  const summary = Object.entries(counts).map(([name, n]) => (n > 1 ? `${name} x${n}` : name)).join(', ');
  const head = h('button', { class: 'head', 'aria-expanded': String(open), onclick: () => toggle(id, open, run[0].ev) },
    dot(running ? 'running' : failed ? 'error' : 'ok'),
    h('span', { class: 'group-title' }, running ? 'Working' : `Used ${run.length} tools`),
    h('span', { class: 'sum' }, summary),
    failed ? h('span', { class: 'fail' }, `${failed} failed`) : null);
  const box = h('div', { class: `group ${running ? 'running' : ''}`, dataset: { depth } }, head);
  if (open) box.append(h('div', { class: 'group-body' }, run.map(n => stepRow(n, depth))));
  return box;
}

function isOpen(id, auto) {
  return opened.has(id) || (auto && !closed.has(id));
}

function toggle(id, wasOpen, ev) {
  if (wasOpen) { opened.delete(id); closed.add(id); } else { closed.delete(id); opened.add(id); }
  dirty.add(keyOf(ev));
  schedule();
}

function dot(state) {
  return h('span', { class: `sdot ${state}`, 'aria-label': state, role: 'img' });
}

/** 'enter' for a row that was not on screen before (it slides in), else ''. */
function fresh(id) {
  if (seen.has(id)) return '';
  seen.add(id);
  return quiet ? '' : 'enter';
}

function stepRow(node, depth) {
  const row = stepEl(node, depth);
  const cls = fresh(node.ev.id);
  if (cls) row.classList.add(cls);
  return row;
}

function stepEl(node, depth) {
  const { ev, kids } = node;
  switch (ev.kind) {
    case 'assistant': return replyRow(ev, kids, depth);
    case 'thinking': return thinkingRow(ev, kids, depth);
    case 'task': return taskRow(ev, kids, depth);
    case 'error': return h('div', { class: 'step err' }, icon(ICONS.alert), h('div', {}, h('strong', {}, 'Problem: '), ev.summary));
    case 'notice': return h('div', { class: 'step note' }, ev.summary, ev.output ? h('span', { class: 'sub' }, ev.output.split('\n')[0]) : null);
    case 'phone': return h('div', { class: 'step note phone' }, h('strong', {}, 'Phone: '), ev.summary);
    case 'voice': return voiceRow(ev);
    case 'heard': return heardRow(ev);
    default: return toolRow(ev, kids, depth);                       // tool, permission
  }
}

const nested = (kids, depth) => (kids.length ? h('div', { class: 'nest' }, renderNodes(kids, depth + 1)) : null);

function toolRow(ev, kids, depth) {
  const id = ev.id;
  const open = isOpen(id, ev.status === 'error');
  const permission = ev.kind === 'permission';
  const row = h('div', { class: `step tool ${ev.status}` },
    h('button', { class: 'head', 'aria-expanded': String(open), onclick: () => toggle(id, open, ev) },
      dot(ev.status),
      h('span', { class: `chip ${permission ? 'perm' : ''}` }, permission ? 'Asked' : (ev.name || 'tool')),
      h('span', { class: 'sum', title: ev.summary }, ev.summary || ''),
      h('span', { class: 'dur' }, ev.status === 'running' ? '' : fmtMs(ev.duration_ms))));
  if (open) {
    if (ev.input) row.append(detail('Input', ev.input, `${id}:in`));
    if (ev.output) row.append(detail(permission ? 'Answer' : 'Result', ev.output, `${id}:out`, permission ? null : ev));
    if (!ev.input && !ev.output) row.append(h('div', { class: 'detail empty-detail' }, ev.status === 'running' ? 'Running...' : 'No details.'));
  }
  const n = nested(kids, depth);
  if (n) row.append(n);
  return row;
}

/** A tool's input or result. `ev`: the tool call, for a result the log cut short (Claude Code's transcript has it whole). */
function detail(label, text, key, ev = null) {
  const LIMIT = 1600;
  const got = ev ? fullOut.get(ev.id) : undefined;
  if (typeof got === 'string') text = got;
  const cut = ev?.kind === 'tool' && got === undefined && backendType === 'claude' ? CLIPPED.exec(text) : null;
  const long = text.length > LIMIT && !fullText.has(key);
  const more = got === LOADING ? h('button', { class: 'plain more', disabled: true }, 'Getting the full output...')
    : cut ? h('button', { class: 'plain more', title: 'From Claude Code\'s transcript of the session', onclick: () => fullOutput(ev, key) },
      `Show full output (${(text.length - cut[0].length + Number(cut[1])).toLocaleString()} characters)`)
      : long ? h('button', { class: 'plain more', onclick: () => { fullText.add(key); rebuildAll(); } }, `Show all (${text.length - LIMIT} more characters)`) : null;
  return h('div', { class: 'detail' },
    h('div', { class: 'label' }, label),
    h('pre', {}, long ? text.slice(0, LIMIT) : text),
    more, got instanceof Error ? h('div', { class: 'detail-note' }, got.message) : null);
}

// --- a result cut short: the whole of it, from Claude Code's transcript (GET /api/transcript/result) -------------------
const CLIPPED = /\n\.\.\. \[(\d+) more characters\]$/;     // how activity.py marks what it cut
const LOADING = Symbol('loading');
const fullOut = new Map();                                  // tool call id -> its whole output, LOADING, or an Error

async function fullOutput(ev, key) {
  fullOut.set(ev.id, LOADING);
  fullText.add(key);
  dirty.add(keyOf(ev));
  schedule();
  try {
    const r = await send(`/api/transcript/result?id=${encodeURIComponent(ev.id)}`);
    fullOut.set(ev.id, r.found ? r.text : new Error('Claude Code\'s transcripts of this folder no longer have it.'));
  } catch (e) {
    fullOut.set(ev.id, new Error(`Couldn't get it: ${e.message}`));
  }
  dirty.add(keyOf(ev));
  schedule();
}

/** The agent's reply, and how the voice said it: a "spoken" mark when word for word, its own words when not. */
function replyRow(ev, kids, depth) {
  const v = spokenAs.get(ev.id), how = v && readsOut.get(v.id);
  const text = h('div', { class: 'text' }, ev.output || '');
  let spoken = null;
  if (v && how.same) {
    text.append(h('span', { class: 'spoken', title: 'The voice read this out word for word' }, icon(ICONS.speaker), 'spoken'));
  } else if (v) {
    const label = v.extra?.interrupted ? (v.output ? 'Cut off after:' : 'Cut off before it began')
      : how.kind === 'followup' ? 'Spoken (only what was new):' : 'Spoken as:';
    spoken = h('div', { class: 'spoken-as' }, h('span', { class: 'chip vchip' }, label), v.output ? h('span', { class: 'vtext' }, v.output) : null);
  }
  return h('div', { class: `step say ${ev.status}` }, text, spoken, nested(kids, depth));
}

function voiceRow(ev) {
  const user = ev.name === 'user';
  const cut = ev.extra?.interrupted ? h('span', { class: 'cut' }, ' (cut off)') : null;
  return h('div', { class: `step voice ${user ? 'user' : ''}` },
    h('span', { class: 'chip vchip' }, user ? 'You said' : 'Voice'), h('span', { class: 'vtext' }, ev.output || ev.summary || ''), cut);
}

// --- what the judge made of something the microphone heard (Settings > Voice > Advanced) ---------------------------------
const HEARD = { drop: 'Ignored', hold: 'Not sure it was for me', stop: 'Heard: stop', approve: 'Heard: yes', decline: 'Heard: no',
  hide: 'Heard: go away', show: 'Heard: come back', compact: 'Heard: compact', new_conversation: 'Heard: new conversation' };
const gone = new Set();                   // held lines the server no longer has

async function sendHeard(ev) {
  try {
    await send('/api/heard/send', 'POST', { id: ev.id });
  } catch {
    gone.add(ev.id);
    dirty.add(keyOf(ev));
    schedule();
  }
}

function heardRow(ev) {
  const x = ev.extra || {};
  const kept = x.action === 'drop' || x.action === 'hold';
  const tail = x.sent ? h('span', { class: 'cut' }, 'sent') : gone.has(ev.id) ? h('span', { class: 'cut' }, 'no longer waiting')
    : kept ? btn('Send', () => sendHeard(ev), { kind: 'ghost sm', title: 'It was for the agent: send it on' }) : null;
  const why = [x.why, x.for_assistant != null ? `for it: ${Math.round(x.for_assistant * 100)}%` : ''].filter(Boolean).join(' · ');
  return h('div', { class: `step voice heard ${x.action || ''}`, title: why },
    h('span', { class: 'chip hchip' }, HEARD[x.action] || 'Heard'), h('span', { class: 'vtext' }, ev.output || ev.summary || ''), tail);
}

function thinkingRow(ev, kids, depth) {
  const open = isOpen(ev.id, false);
  const first = (ev.output || '').trim().split('\n')[0].slice(0, 120);
  return h('div', { class: 'step think' },
    h('button', { class: 'head', 'aria-expanded': String(open), onclick: () => toggle(ev.id, open, ev) },
      h('span', { class: 'chip soft' }, 'Thinking'), open ? null : h('span', { class: 'sum' }, first)),
    open ? h('div', { class: 'detail' }, h('pre', {}, ev.output || '')) : null, nested(kids, depth));
}

function taskRow(ev, kids, depth) {
  const x = ev.extra || {};
  const meta = [x.last_tool ? `last: ${x.last_tool}` : '', x.tool_uses ? `${x.tool_uses} tool calls` : '',
    x.tokens ? `${fmtTokens(x.tokens)} tokens` : '', ev.status !== 'running' ? fmtMs(ev.duration_ms) : ''].filter(Boolean).join('  ·  ');
  const open = isOpen(ev.id, ev.status === 'running');
  return h('div', { class: `step task ${ev.status}` },
    h('button', { class: 'head', 'aria-expanded': String(open), onclick: () => toggle(ev.id, open, ev) },
      dot(ev.status), h('span', { class: 'chip bgchip' }, ev.name || 'task'), h('span', { class: 'sum', title: ev.summary }, ev.summary || '')),
    meta ? h('div', { class: 'meta' }, meta) : null,
    open && ev.output ? detail('Result', ev.output, `${ev.id}:out`) : null, nested(kids, depth));
}

// --- the question on the table --------------------------------------------------------------------------------------
function showSession(s) {
  const rt = s.realtime;
  const [state, voice] = !rt ? ['classic', s.voice_mode === 'realtime' ? 'Classic voice (in a room)' : 'Classic voice']
    : [rt.state, { connected: rt.label, connecting: `Connecting to ${rt.label}`, retrying: `Reconnecting to ${rt.label}`,
        failed: `${rt.label} unavailable`, off: 'Voice off' }[rt.state] || `${rt.label}: ${rt.state}`];
  const folder = (s.cwd || '').split(/[\\/]/).filter(Boolean).pop() || s.cwd || '';
  $('session').replaceChildren(...[
    h('span', { class: 'schip voice', dataset: { state }, title: rt?.error || 'The voice' }, h('span', { class: 'vdot' }), voice),
    h('span', { class: 'schip sess', title: s.session_id ? `Session ${s.session_id}` : 'A new conversation starts with your next message' },
      s.session_id ? `Session ${s.session_id.slice(0, 8)}` : 'New session'),
    s.session_cost_usd ? h('span', { class: 'schip cost', title: 'Cost of this session so far' }, fmtCost(s.session_cost_usd)) : null,
    contextChip(s),
    h('span', { class: 'spacer' }),
    folder ? h('button', { class: 'schip folder', title: `Working in ${s.cwd}. Click to change it.`, onclick: () => openTab('agent#folder') },
      icon(ICONS.folder), h('span', {}, folder)) : null].filter(Boolean));
}

/** How full the agent's context window is, when it says (the conversation is compacted while idle at compact_at %). */
function contextChip(s) {
  const c = s.context;
  if (!c || !c.size) return null;
  const pct = Math.min(100, Math.round(100 * c.used / c.size)), at = s.compact_at;
  return h('span', { class: 'schip ctx', dataset: { level: at && pct >= at - 10 ? 'high' : '' },
    title: `Context: ${fmtTokens(c.used)} of ${fmtTokens(c.size)} tokens` +
      (at ? `. The conversation is compacted while idle once it is ${at}% full.` : '') }, `Context ${pct}%`);
}

export function setStatus(s) {
  showSession(s);
  if ((s.backend?.type || null) !== backendType) {
    backendType = s.backend?.type || null;
    applyView();
    rebuildAll();                                         // "Show full output" is only for Claude Code
  }
  const p = s.permission, box = $('perm');
  if (!p) { box.hidden = true; box.replaceChildren(); box.dataset.id = ''; return; }
  if (box.dataset.id === p.id) return;
  box.dataset.id = p.id;
  box.hidden = false;
  const answer = (allow, always = false) => () => send('/api/permission', 'POST', { id: p.id, allow, always }).catch(() => {});
  box.replaceChildren(
    h('div', { class: 'perm-head' }, icon(ICONS.shield), h('strong', {}, 'Waiting for you')),
    h('div', { class: 'ask' }, p.text),
    h('div', { class: 'row' },
      h('button', { class: 'btn sm', onclick: answer(false) }, 'Deny'),
      p.can_always ? h('button', { class: 'btn sm', onclick: answer(true, true) }, 'Always allow') : null,
      h('button', { class: 'btn primary sm', onclick: answer(true) }, 'Allow')));
}

/** The tab came into view (or the drawer unfolded): back to the latest, if that is where it was. */
export function shown() {
  if (view === 'raw') transcript.shown();
  else if (follow) $('log').scrollTop = $('log').scrollHeight;
}

// --- the view: the summary, or Claude Code's own transcript ------------------------------------------------------------
// `wanted` is what was asked for (remembered); Raw is only shown while the agent is Claude Code, whose session file it is.
let view = 'summary', wanted = 'summary', backendType = null;
try { wanted = localStorage.getItem('panel.activityView') === 'raw' ? 'raw' : 'summary'; } catch { /* fine */ }

/** Show the summary or the raw transcript ("raw"); the panel's "activity#transcript" asks for the second. */
export function setView(v) {
  wanted = v === 'raw' ? 'raw' : 'summary';
  try { localStorage.setItem('panel.activityView', wanted); } catch { /* fine */ }
  applyView();
}

function applyView() {
  const can = backendType === 'claude';
  view = wanted === 'raw' && can ? 'raw' : 'summary';
  const raw = view === 'raw';
  $('views').hidden = !can;
  $('tab-activity').classList.toggle('raw', raw);
  for (const id of ['filters', 'clear', 'log']) $(id).hidden = raw;
  document.querySelector('#toolbar .searchbox').hidden = raw;
  $('ttools').hidden = $('tlog').hidden = !raw;
  for (const b of document.querySelectorAll('#views button')) {
    b.classList.toggle('on', b.dataset.view === view);
    b.setAttribute('aria-pressed', String(b.dataset.view === view));
  }
  requestAnimationFrame(shown);
}

// --- controls ---------------------------------------------------------------------------------------------------
export function init(open) {
  openTab = open;
  const log = $('log');
  log.addEventListener('scroll', () => {
    follow = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    $('jump').hidden = follow;
  });
  $('jump').addEventListener('click', () => { follow = true; log.scrollTop = log.scrollHeight; $('jump').hidden = true; });
  for (const b of document.querySelectorAll('#views button')) b.addEventListener('click', () => setView(b.dataset.view));
  applyView();
  for (const b of document.querySelectorAll('#filters button')) {
    b.addEventListener('click', () => {
      filter = b.dataset.filter;
      for (const o of document.querySelectorAll('#filters button')) o.classList.toggle('on', o === b);
      rebuildAll();
    });
  }
  let t = 0;
  $('search').addEventListener('input', e => {
    clearTimeout(t);
    t = setTimeout(() => { query = e.target.value.trim().toLowerCase(); rebuildAll(); }, 150);
  });
  $('clear').addEventListener('click', () => {
    clearedBefore = Date.now() / 1000;
    for (const el of blocks.values()) el.remove();
    blocks.clear();
    rebuildAll();
  });
}
