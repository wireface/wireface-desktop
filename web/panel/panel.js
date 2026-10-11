// The panel window (panel.html): the agent's activity log (with its full transcript as the Raw view), its tools (MCP
// servers and skills), and this face's settings (Face, Voice, Agent). In the desktop shell it is its own window,
// floating or docked to a screen edge as a drawer that folds into a slim tab; in a plain browser it is just a page.
import { $, SHELL, TOKEN } from './common.js';
import * as activity from './activity.js';
import * as transcript from './transcript.js';
import * as tools from './tools.js';
import * as settings from './settings.js';
import * as face from './face.js';
import * as phone from './phone.js';

document.body.classList.toggle('browser', !SHELL);

// --- tabs ---------------------------------------------------------------------------------------------------------------
// "settings" (the face's menu) opens whichever settings tab was used last; an anchor after # picks out one setting.
// "activity#transcript" opens the Activity tab on its Raw view, "activity#summary" on the summary ("transcript" on its
// own, the tab it used to be, does the same as the first).
const TABS = ['activity', 'tools', 'phone', 'face', 'voice', 'agent'];
const SETTINGS = ['face', 'voice', 'agent'];
const SETTINGS_ANCHORS = { skip: 'agent', folder: 'agent', room: 'agent', speech: 'voice', phone: 'agent' };
let tab = null;

function lastSettings() {
  try { return localStorage.getItem('panel.settingsTab') || 'face'; } catch { return 'face'; }
}

function showTab(spec) {
  let [name, anchor] = String(spec || 'activity').split('#');
  if (name === 'settings') name = SETTINGS_ANCHORS[anchor] || lastSettings();
  if (name === 'transcript') [name, anchor] = ['activity', 'transcript'];
  if (name === 'activity' && (anchor === 'transcript' || anchor === 'raw' || anchor === 'summary')) {
    activity.setView(anchor === 'summary' ? 'summary' : 'raw');
  }
  if (!TABS.includes(name)) name = 'activity';
  const changed = name !== tab;
  tab = name;
  for (const b of document.querySelectorAll('#tabs [role=tab]')) {
    const on = b.dataset.tab === name;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  }
  for (const t of TABS) $(`tab-${t}`).hidden = t !== name;
  requestAnimationFrame(moveInk);                       // after the selected tab has taken its (wider) place
  if (SETTINGS.includes(name)) { try { localStorage.setItem('panel.settingsTab', name); } catch { /* fine */ } }
  if (name === 'phone') phone.shown(); else phone.hidden();
  if (name === 'face') face.shown(anchor);
  else if (name === 'voice' || name === 'agent') settings.shown(anchor);
  else if (name === 'tools') tools.shown(anchor);
  else if (changed) activity.shown();
}

/** The sliding highlight under the selected tab. */
function moveInk() {
  const b = document.querySelector('#tabs [aria-selected=true]'), ink = $('tab-ink');
  if (!b) return;
  ink.style.transform = `translateX(${b.offsetLeft}px)`;
  ink.style.width = `${b.offsetWidth}px`;
}
addEventListener('resize', moveInk);

const tabButtons = [...document.querySelectorAll('#tabs [role=tab]')];
for (const b of tabButtons) {
  b.addEventListener('click', () => showTab(b.dataset.tab));
  b.addEventListener('keydown', e => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    const next = tabButtons[(tabButtons.indexOf(b) + step + tabButtons.length) % tabButtons.length];
    next.focus();
    showTab(next.dataset.tab);
  });
}
const tabFromHash = () => new URLSearchParams(location.hash.slice(1)).get('tab');
if (SHELL) SHELL.onPanelTab(showTab);
addEventListener('hashchange', () => showTab(tabFromHash()));       // a browser popup asked for another tab

// --- window buttons, and the drawer -----------------------------------------------------------------------------------
let dock = null, collapsed = false;

/** Docked to an edge (or floating), open or folded into the slim tab: the page follows the window. */
function applyDock(d) {
  const unfolded = collapsed && !(d?.dock && d.collapsed);
  dock = d?.dock || null;
  collapsed = !!(dock && d.collapsed);
  const b = document.body;
  b.classList.toggle('docked', !!dock);
  b.classList.toggle('dock-left', dock === 'left');
  b.classList.toggle('dock-right', dock === 'right');
  b.classList.toggle('collapsed', collapsed);
  $('rail').hidden = !collapsed;
  $('app').inert = collapsed;
  if (!collapsed) $('rail').classList.remove('fresh');
  $('w-fold').hidden = !dock;
  $('w-fold').title = 'Fold away to the edge';
  $('w-max').hidden = $('w-min').hidden = !!dock;
  $('w-dock').classList.toggle('on', !!dock);
  $('w-dock').title = dock ? `Docked ${dock}: click to dock elsewhere or float` : 'Dock to a screen edge';
  for (const m of $('dockmenu').querySelectorAll('[data-dock]')) m.setAttribute('aria-checked', String((m.dataset.dock || null) === dock));
  if (!collapsed) requestAnimationFrame(moveInk);
  if (unfolded && tab === 'activity') requestAnimationFrame(() => activity.shown());   // hidden while folded: scrolled back to the top
}

function dockMenu(open) {
  const menu = $('dockmenu'), btn = $('w-dock');
  const show = open ?? menu.hidden;
  menu.hidden = !show;
  btn.setAttribute('aria-expanded', String(show));
  if (!show) return;
  const r = btn.getBoundingClientRect();
  menu.style.top = `${r.bottom + 6}px`;
  menu.style.left = `${Math.max(8, Math.min(r.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 8))}px`;
  (menu.querySelector('[aria-checked=true]') || menu.querySelector('button')).focus();
}

if (SHELL) {
  let pinned = true;
  const setPinned = on => { pinned = on; $('w-pin').setAttribute('aria-pressed', String(on)); $('w-pin').classList.toggle('on', on); };
  SHELL.panelInit().then(i => { setPinned(i ? i.pinned : true); applyDock(i); });
  SHELL.onPanelDock?.(d => { applyDock(d); if (d?.dock) setPinned(true); });
  $('w-pin').addEventListener('click', () => { setPinned(!pinned); SHELL.panelPin(pinned); });
  $('w-min').addEventListener('click', () => SHELL.panelMinimize());
  $('w-max').addEventListener('click', () => SHELL.panelMaximize());
  $('w-close').addEventListener('click', () => SHELL.panelClose());
  $('w-dock').addEventListener('click', e => { e.stopPropagation(); dockMenu(); });
  $('w-fold').addEventListener('click', () => SHELL.panelCollapse(true));
  for (const m of $('dockmenu').querySelectorAll('[data-dock]')) {
    m.addEventListener('click', () => { dockMenu(false); SHELL.panelDock(m.dataset.dock || null); });
  }
  $('rail').addEventListener('click', () => SHELL.panelCollapse(false));
  addEventListener('pointerdown', e => { if (!$('dockmenu').hidden && !e.target.closest('#dockmenu, #w-dock')) dockMenu(false); });
  addEventListener('keydown', e => { if (e.key === 'Escape' && !$('dockmenu').hidden) { dockMenu(false); $('w-dock').focus(); } });
  addEventListener('blur', () => dockMenu(false));
}

// --- toasts -----------------------------------------------------------------------------------------------------------------
let toastTimer = 0;
export function toast(text, ms = 5000) {
  const el = $('toast');
  el.textContent = text;
  el.classList.remove('show');
  void el.offsetWidth;                                     // restart the entrance
  el.classList.add('show');
  clearTimeout(toastTimer);
  if (ms) toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}
settings.setToast(toast);
$('toast').addEventListener('click', () => $('toast').classList.remove('show'));

// --- this face's colours --------------------------------------------------------------------------------------------------
const luminance = hex => {
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(c => (c <= 0.04 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
function applyColors(c) {
  const st = document.documentElement.style;
  st.setProperty('--p', c.primary);
  st.setProperty('--s', c.secondary);
  st.setProperty('--bgc', c.background);
  st.setProperty('--on-p', luminance(c.primary) > 0.3 ? '#14100c' : '#ffffff');     // text on the accent colour
}

// --- status -------------------------------------------------------------------------------------------------------------------
function onStatus(s) {
  const b = s.beacon || { state: 'offline', label: 'offline' };
  for (const id of ['mark', 'rail-mark']) {
    $(id).dataset.state = b.state;
    $(id).classList.toggle('skip', !!b.skip_permissions);
  }
  $('pstate').textContent = `${s.agent || 'Claude'}  ·  ${b.label}`;               // the title is the app's; the face's name goes here
  $('pstate').title = $('pstate').textContent;
  $('rail-label').textContent = `${s.agent || 'Claude'}  ·  ${b.label}`;
  $('rail').title = `${b.label}. Click to open.`;
  $('skipbadge').hidden = !b.skip_permissions;
  $('taskbadge').hidden = !(b.tasks > 0) || b.state === 'background';         // (the label says it already)
  $('taskbadge').textContent = `${b.tasks} task${b.tasks === 1 ? '' : 's'}`;
  $('taskbadge').title = `${b.tasks} background task${b.tasks === 1 ? '' : 's'} running`;
  $('rail').classList.toggle('ask', b.state === 'needs_input');
  const room = s.room, rb = $('roombadge');
  rb.hidden = !room;
  if (room) {
    const who = !room.connected ? 'connecting' : room.floor ? `${room.floor} speaking` : room.lead ? `${room.lead} leading` : `${room.members.length} here`;
    rb.textContent = `${room.name}: ${who}`;
    rb.title = room.connected ? `Room ${room.name}, with: ${room.members.join(', ')}` : `Joining the room ${room.name}`;
  }
  activity.setStatus(s);
  transcript.setStatus(s);
  tools.status(s);
  settings.status(s);
  const phoneOn = !!s.phone && s.phone.state !== 'off';            // the Phone tab is there while the line is on
  if ($('tab-btn-phone').hidden === phoneOn) { $('tab-btn-phone').hidden = !phoneOn; requestAnimationFrame(moveInk); }
  if (!phoneOn && tab === 'phone') showTab('activity');
}

/** New transcript entries: a pulse on the folded drawer. */
function fresh() {
  if (collapsed) $('rail').classList.add('fresh');
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'status') onStatus(msg);
    else if (msg.type === 'activity') { activity.upsert(msg.event); phone.activity(msg.event); }
    else if (msg.type === 'transcript') transcript.append(msg.entries);
    else if (msg.type === 'transcript_reset') transcript.reset(msg);
    else if (msg.type === 'tools') tools.update(msg.view);
    else if (msg.type === 'face_config') { applyColors(msg.config.colors); face.config(msg.config); }
    else if (msg.type === 'notice') toast(msg.text);
    else if (msg.type === 'models') settings.models(msg);
    else if (msg.type === 'meter') settings.meter(msg);
  };
  ws.onopen = () => {                                       // anything missed while disconnected
    activity.load().catch(() => {});
    transcript.load().catch(() => {});
  };
  ws.onclose = () => {
    $('mark').dataset.state = 'offline';
    $('pstate').textContent = 'Reconnecting...';
    setTimeout(connect, 1500);
  };
}

async function boot() {
  activity.init(name => showTab(name));
  transcript.init({ onFresh: fresh, summary: () => activity.setView('summary') });
  tools.init({ toast, openTab: name => showTab(name) });
  const [, look] = await Promise.all([settings.init(), face.init(toast)]);
  applyColors(look.colors);
  showTab(tabFromHash());
  connect();
  requestAnimationFrame(() => document.body.classList.add('ready'));   // transitions on only after the first layout
}
boot().catch(e => toast(`Can't reach the face: ${e.message}`, 0));
