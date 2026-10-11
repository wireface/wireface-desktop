// The face window (face.html): floating face or floating panel. In the desktop shell (Electron),
// `window.faceShell` moves/sizes the window and shows native menus; in a plain browser it's a page.
import { FaceView, FACE_CENTER_Y } from './face.js';
import { VoiceMeter } from './meter.js';
import { preparePhoto } from './photo.js';

const hash = new URLSearchParams(location.hash.slice(1));
const TOKEN = hash.get('t') || sessionStorage.getItem('faceToken') || '';
if (hash.get('t')) sessionStorage.setItem('faceToken', TOKEN);
const SHELL = window.faceShell || null;
const $ = id => document.getElementById(id);
document.body.classList.toggle('browser', !SHELL);

/** A text-only element: whatever is shown here (replies, transcripts, errors) never goes in as HTML. */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const api = (path, opts = {}) => fetch(path, {
  ...opts, headers: { 'X-Face-Token': TOKEN, ...(opts.body ? { 'Content-Type': 'application/json' } : {}) },
});
async function send(path, method = 'POST', body) {
  const r = await api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
const putFace = patch => send('/api/face', 'PUT', patch).catch(e => notice(e.message));
const setMode = patch => send('/api/mode', 'POST', patch).catch(e => notice(e.message));

const canvas = $('face');
const view = new FaceView(canvas, { api });
let cfg = view.cfg;
let status = null;
let mode = SHELL ? 'floating' : 'windowed';
let speaking = false;

// --- mode, config -----------------------------------------------------------------------------------
function setBodyMode(m) {
  document.body.classList.toggle('mode-floating', m === 'floating');
  document.body.classList.toggle('mode-windowed', m !== 'floating');
  view.zoomKey = m === 'floating' ? null : 'zoom';
  view.fit = m === 'floating' ? 0.95 : 0.86;          // the panel leaves room for captions
  // floating has no frame to hide behind: centre a little low and leave room for nods, looking down and an open jaw
  view.centerY = m === 'floating' ? -1 : FACE_CENTER_Y;
  ignoring = null;
}

const luminance = hex => {
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(c => (c <= 0.04 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

function applyConfig(c) {
  cfg = c;
  showSkin(c);
  const st = document.documentElement.style;
  st.setProperty('--p', c.colors.primary);
  st.setProperty('--s', c.colors.secondary);
  st.setProperty('--bgc', c.colors.background);
  st.setProperty('--on-p', luminance(c.colors.primary) > 0.3 ? '#14100c' : '#ffffff');     // text on the accent colour
  $('bg').className = c.background;
  if (SHELL) {
    if (c.mode !== mode) { mode = c.mode; SHELL.setMode(mode); }
    SHELL.setOnTop(!!c.on_top);
  }
  setBodyMode(mode);
  if (!c.captions) $('caption').classList.remove('show');
}

// --- the skin -------------------------------------------------------------------------------------------------
// The face wears a photo cross-faded over its wireframe by skin_amount (the engine glides to each new value, so the
// panel's slider can send a few a second as it moves). A new photo sweeps on from the top down once the old one has
// swept off.
let wearing;                       // the photo version on the face; undefined until the first config
let skinTurn = 0;                  // the latest change of photo: one still under way gives way to it
let offPhoto = null;               // the old photo, kept on while it sweeps off

function showSkin(c) {
  if ((c.photo?.version || null) !== wearing) changePhoto(c, wearing === undefined);
  else putOn(c);
}

/** view.setConfig, with a photo that is sweeping off still on. */
function putOn(c) {
  view.setConfig(offPhoto ? { ...c, photo: offPhoto } : c);
}

async function changePhoto(c, first) {
  const turn = ++skinTurn;
  wearing = c.photo?.version || null;
  if (!first && view.sweepAt > 0 && view.cfg.photo) {
    offPhoto ??= view.cfg.photo;
    putOn(c);
    await view.sweep(0, 1.1);
    if (turn !== skinTurn) return;
    offPhoto = null;
  } else view.sweepAt = 0;         // the wireframe until the new photo has loaded
  putOn(cfg);
  await Promise.race([view.photoLoading, new Promise(r => setTimeout(r, 5000))]);    // a slow one pops in when it comes
  if (turn === skinTurn) view.sweep(1);
}

// --- status ---------------------------------------------------------------------------------------------
const STATES = {
  starting: ['#e8a317', 'waking up'], ready: ['#22b573', 'ready'], thinking: ['#3b86f0', 'thinking'],
  speaking: ['#8b5cf6', 'speaking'], error: ['#e5484d', 'needs attention'], offline: ['#8a8f98', 'reconnecting'],
};
function showState(color, text) {
  const dot = $('pill').querySelector('.dot');
  dot.style.background = color;
  dot.style.color = color;
  $('state-text').textContent = text;
}
function onStatus(s) {
  status = s;
  const m = s.mode || {};
  const [color, label] = STATES[s.state] || STATES.offline;
  showState(color, s.busy_op || (s.state === 'ready' && m.listening ? 'listening' : label));
  $('name').textContent = s.agent || 'Claude';
  $('chat-input').placeholder = `Message ${s.agent || 'Claude'}...`;
  for (const [id, on] of [['t-mic', m.listening], ['t-voice', m.voice]]) $(id).classList.toggle('on', !!on);
  $('t-stop').disabled = !(s.flags?.speaking || s.flags?.thinking);
  showPermission(s.permission);
  setCompacting(!!s.compacting);
  showDetector(s);
}

// --- the voice detector: how far what the microphone hears stands out from the room (Settings > Voice > Advanced) ---
const meter = new VoiceMeter($('meter-canvas'), { seconds: 8, compact: true });
function showDetector(s) {
  const on = !!s.detector;
  if (on === !$('meter').hidden) {
    if (on) meter.idle = s.room ? 'The room is listening' : !s.mode?.listening ? 'Not listening' : '';
    return;
  }
  $('meter').hidden = !on;
  document.body.classList.toggle('detector', on);
  if (on) {
    meter.idle = s.room ? 'The room is listening' : !s.mode?.listening ? 'Not listening' : '';
    meter.start();
  } else {
    meter.stop();
    meter.clear();
  }
}

// --- while the agent compacts its conversation: the eyes glow with a spinner, and the face says so on hover ---------
let compacting = false, lastPointer = null;
function setCompacting(on) {
  if (on === compacting) return;
  compacting = on;
  view.setSpinner(on);
  hoverTip();
}
/** The pointer at (x, y) in the window: over the face while it compacts, a tooltip follows it. */
function hoverTip(x = lastPointer?.[0], y = lastPointer?.[1]) {
  if (x != null) lastPointer = [x, y];
  const tip = $('tip'), r = canvas.getBoundingClientRect();
  const over = compacting && x != null && view.hitTest(x - r.left, y - r.top, 6);
  tip.classList.toggle('show', !!over);
  if (!over) return;
  tip.textContent = 'Compacting the conversation, please wait...';
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const left = Math.max(4, Math.min(innerWidth - w - 4, x - w / 2));
  const top = y + 22 + h <= innerHeight - 4 ? y + 22 : Math.max(4, y - h - 12);
  tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
}

// --- the panel window: the agent's state, its activity (and Claude Code's raw transcript), tools and the settings --
/** Open it (or unfold the docked drawer) on a tab: activity, tools, phone, face, voice, agent, or settings (the one used
 * last); "#x" picks out a setting. */
function openPanel(tab) {
  if (SHELL) SHELL.togglePanel(tab, false);
  else window.open(`/panel#t=${encodeURIComponent(TOKEN)}&tab=${encodeURIComponent(tab)}`, 'face-panel', 'popup,width=520,height=720');
}

// --- captions ---------------------------------------------------------------------------------------------
// Shown in the panel; in the floating view only for replies that aren't spoken (voice off).
let capTimer = 0;
function caption(text, cls, holdMs, always = false) {
  const box = $('caption');
  clearTimeout(capTimer);
  if (!text || (!always && (!cfg.captions || mode === 'floating'))) { box.classList.remove('show'); return; }
  box.replaceChildren(el('span', cls, text));
  box.classList.add('show');
  if (holdMs) capTimer = setTimeout(() => box.classList.remove('show'), holdMs);
}
/** Leave what is showing up for a little longer, then fade it. */
function holdCaption(ms) {
  clearTimeout(capTimer);
  capTimer = setTimeout(() => $('caption').classList.remove('show'), ms);
}
view.on(ev => {
  if (ev.type === 'caption') caption(ev.text, 'said');
  if (ev.type === 'speaking') {
    speaking = !!ev.text;
    if (!speaking) holdCaption(1800);
  }
});
function onEvent(ev) {
  if (ev.topic === 'face.visibility') { setAway(ev.payload?.visible === false, false); return; }
  if (speaking) return;
  if (ev.topic === 'percept.audio.transcript' && ev.payload?.text) caption(`“${ev.payload.text}”`, 'you', 3000);
  // the judge (Settings > Voice > Advanced) decided it wasn't for the agent, or isn't sure: shown faded
  if (ev.topic === 'percept.audio.judged' && ev.payload?.text) caption(`“${ev.payload.text}”`, 'you kept', 1500);
}
// voice off: the reply arrives as text, a piece at a time; each stays up long enough to read
let sayQueue = [], sayBusy = false;
function say(text) {
  sayQueue.push(text);
  if (sayBusy) return;
  sayBusy = true;
  const next = () => {
    const t = sayQueue.shift();
    if (t === undefined) { sayBusy = false; return; }
    const ms = Math.max(1500, Math.min(9000, t.length * 55));
    caption(t, 'said', ms + 1500, true);
    setTimeout(next, sayQueue.length ? ms : 0);
  };
  next();
}

// --- notices -------------------------------------------------------------------------------------------------
let noticeTimer = 0, noticeHold = false;
function closeNotice() {
  $('notice').classList.remove('show');
  if (noticeHold) { noticeHold = false; interact(false); }
}
function notice(text, buttons = null, holdMs = 4000) {
  const box = $('notice');
  clearTimeout(noticeTimer);
  closeNotice();
  box.replaceChildren(el('div', 'ntext', text));
  if (buttons) {
    const row = el('div', 'row');
    for (const [label, fn, primary] of buttons) {
      const b = el('button', primary ? 'primary' : '', label);
      b.onclick = () => { closeNotice(); fn(); };
      row.append(b);
    }
    box.append(row);
    noticeHold = true;
    interact(true);
  } else if (holdMs) noticeTimer = setTimeout(closeNotice, holdMs);
  box.classList.add('show');
}

// --- permission prompts: Claude Code asking before it runs or changes something --------------------------------
let permissionShown = null;
function showPermission(p) {
  if ((p?.id || null) === permissionShown) return;
  if (permissionShown) closeNotice();
  permissionShown = p?.id || null;
  if (!p) return;
  const answer = (allow, always = false) => () => {
    permissionShown = null;
    send('/api/permission', 'POST', { id: p.id, allow, always }).catch(e => notice(e.message));
  };
  notice(p.text, [['Deny', answer(false)], ...(p.can_always ? [['Always allow', answer(true, true)]] : []), ['Allow', answer(true), true]]);
}

// --- click-through (floating): only the face itself takes the mouse ------------------------------------------
let ignoring = null, hold = 0;
function interact(on) {            // something needs the mouse (chat box, dialog): stop passing clicks through
  hold = Math.max(0, hold + (on ? 1 : -1));
  updateHit(-1e4, -1e4);
}
function updateHit(x, y) {
  if (!SHELL) return;
  let ignore = false;
  if (mode === 'floating' && !hold && !dragging) {
    const r = canvas.getBoundingClientRect();
    ignore = !view.hitTest(x - r.left, y - r.top, 6);
  }
  if (ignore !== ignoring) { ignoring = ignore; SHELL.setIgnoreMouse(ignore); }
}
document.addEventListener('mousemove', e => { updateHit(e.clientX, e.clientY); hoverTip(e.clientX, e.clientY); });

// eyes follow the pointer: anywhere on screen in the shell, inside the page in a browser
function pointTo(x, y) {
  const r = canvas.getBoundingClientRect(), b = view.bounds();
  const fx = r.left + (b ? b.cx : r.width / 2), fy = r.top + (b ? b.cy - (b.bottom - b.top) * 0.12 : r.height / 2);
  view.setPointer(x - fx, y - fy);
}
if (SHELL) SHELL.onCursor(p => { pointTo(p.x, p.y); updateHit(p.x, p.y); hoverTip(p.x, p.y); });
else {
  document.addEventListener('mousemove', e => pointTo(e.clientX, e.clientY));
  document.addEventListener('mouseleave', () => { view.setPointer(null); lastPointer = null; hoverTip(); });
}

// --- drag to move, double-click to switch view ------------------------------------------------------------
let down = null, dragging = false;
function startPress(e) {
  if (e.button !== 0 || e.target.closest('button, input, form, #notice, #grip')) return;
  if (mode === 'floating') {
    const r = canvas.getBoundingClientRect();
    if (!view.hitTest(e.clientX - r.left, e.clientY - r.top, 6)) return;
  }
  down = [e.screenX, e.screenY];
  e.currentTarget.setPointerCapture(e.pointerId);
}
for (const elm of [$('stage'), $('bar')]) {
  elm.addEventListener('pointerdown', startPress);
  elm.addEventListener('pointermove', e => {
    if (!down || dragging) return;
    if (Math.hypot(e.screenX - down[0], e.screenY - down[1]) > 4) { dragging = true; SHELL?.drag(true); }
  });
  const end = () => { if (dragging) SHELL?.drag(false); dragging = false; down = null; };
  elm.addEventListener('pointerup', end);
  elm.addEventListener('pointercancel', end);
  elm.addEventListener('lostpointercapture', end);
}
$('stage').addEventListener('dblclick', e => {
  if (!SHELL || e.target.closest('#notice, #chat')) return;
  putFace({ mode: mode === 'floating' ? 'windowed' : 'floating' });
});
window.addEventListener('blur', () => { if (dragging) { SHELL?.drag(false); dragging = false; down = null; } });

// resize grip (windowed)
$('grip').addEventListener('pointerdown', e => { e.currentTarget.setPointerCapture(e.pointerId); SHELL?.resize(true); });
for (const t of ['pointerup', 'pointercancel', 'lostpointercapture']) $('grip').addEventListener(t, () => SHELL?.resize(false));

// --- zoom: scroll over the face to resize it ---------------------------------------------------------------------
let zoomSave = 0;
function zoom(factor) {
  if (mode === 'floating') { SHELL?.zoom(factor); return; }
  const z = Math.max(0.5, Math.min(2, (cfg.zoom || 1) * factor));
  cfg = { ...cfg, zoom: z };
  view.cfg.zoom = z;
  clearTimeout(zoomSave);
  zoomSave = setTimeout(() => putFace({ zoom: Math.round(z * 100) / 100 }), 500);
}
$('stage').addEventListener('wheel', e => {
  e.preventDefault();
  const f = e.deltaY < 0 ? 1.08 : 1 / 1.08;
  if (mode !== 'floating' && e.ctrlKey) SHELL?.zoom(f);   // ctrl+wheel resizes the panel itself
  else zoom(f);
}, { passive: false });

// --- toolbar --------------------------------------------------------------------------------------------------
const halt = () => send('/api/halt').catch(() => {});
const newConversation = () => send('/api/session/new').catch(e => notice(e.message));
const compactConversation = () => send('/api/session/compact')
  .then(() => notice('Compacting the conversation.')).catch(e => notice(e.message));
function quit() {
  if (SHELL) SHELL.quit();
  else notice('Close this tab; stop the backend with Ctrl+C in its window.');
}
$('t-mic').onclick = () => status && setMode({ listening: !status.mode.listening });
$('t-voice').onclick = () => status && setMode({ voice: !status.mode.voice });
$('t-stop').onclick = halt;
$('t-chat').onclick = () => openChat(!$('chat').classList.contains('open'));
$('btn-new').onclick = newConversation;
$('btn-settings').onclick = () => openPanel('settings');
$('btn-float').onclick = () => putFace({ mode: 'floating' });
$('btn-quit').onclick = quit;

// --- chat -----------------------------------------------------------------------------------------------------
function openChat(open = true) {
  const form = $('chat');
  if (open === form.classList.contains('open')) { if (open) $('chat-input').focus(); return; }
  form.classList.toggle('open', open);
  $('t-chat').classList.toggle('on', open);
  interact(open);
  if (open) { SHELL?.focus(); setTimeout(() => $('chat-input').focus(), 30); }
}
const chat = text => send('/api/chat', 'POST', { text }).catch(err => notice(err.message));
$('chat').addEventListener('submit', async e => {
  e.preventDefault();
  const input = $('chat-input'), text = input.value.trim();
  if (!text) return;
  input.value = '';
  await chat(text);
  if (mode === 'floating') openChat(false);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (document.querySelector('.hmenu')) closeMenu();
    else if ($('chat').classList.contains('open')) openChat(false);
    else halt();
  } else if (e.ctrlKey && (e.key === '=' || e.key === '+')) zoom(1.1);
  else if (e.ctrlKey && e.key === '-') zoom(1 / 1.1);
});

// --- photo ------------------------------------------------------------------------------------------------------
async function usePhoto(file) {
  notice('Finding the face in that picture...', null, 0);
  try {
    const p = await preparePhoto(file);
    await send('/api/face/photo', 'PUT', p);
    notice('Done: that photo is now my face. More choices are under Settings > Face.');
  } catch (e) { notice(e.message, null, 6000); }
}

/** A face the agent drew for itself (its set_face tool): its landmarks are found here, as for a dropped photo, and
 *  sent back for the server to put it on. Any error goes back too, for the agent to hear. */
async function mapDrawn(id) {
  const path = `/api/face/drawn/${encodeURIComponent(id)}`;
  const r = await api(path).catch(() => null);
  if (!r?.ok) return;                                   // gone already: another face window put it on
  let found;
  try { found = await preparePhoto(await r.blob()); }
  catch (e) { found = { error: e.message }; }
  await send(path, 'POST', found).catch(() => {});
}

// --- drag & drop: a photo becomes the face; text and files go to the agent ------------------------------------
let dragDepth = 0;
document.addEventListener('dragenter', e => { e.preventDefault(); if (!dragDepth++) { document.body.classList.add('dragging'); interact(true); } });
document.addEventListener('dragleave', () => { if (--dragDepth <= 0) endDrag(); });
document.addEventListener('dragover', e => e.preventDefault());
function endDrag() {
  if (document.body.classList.contains('dragging')) interact(false);
  dragDepth = 0;
  document.body.classList.remove('dragging');
}
document.addEventListener('drop', async e => {
  e.preventDefault();
  endDrag();
  const file = e.dataTransfer.files?.[0];
  if (file && file.type.startsWith('image/')) {
    notice(`Use “${file.name}” as my face?`, [['Cancel', () => {}], ['Send it to me instead', () => sendFile(file)], ['Use photo', () => usePhoto(file), true]]);
  } else if (file) sendFile(file);
  else {
    const text = (e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain') || '').trim();
    if (text) { await chat(text.slice(0, 8000)); notice('Sent.', null, 1500); }
  }
});
async function sendFile(file) {
  const path = SHELL?.pathForFile(file) || file.name;
  await chat(`Here's a file for you: ${path}`);
  notice(`Sent the file path to ${status?.agent || 'Claude'}.`, null, 2000);
}

// --- going away and coming back ---------------------------------------------------------------------------------
// "Go away" (said to the face, the agent's face tool, the tray icon, this menu): the face burns away, then the window
// hides; "come back" brings it back the same way, backwards. The agent keeps working and talking meanwhile.
let away = false;
async function setAway(on, tell = true) {
  if (on === away) return;
  away = on;
  document.body.classList.toggle('away', on);
  if (tell) send('/api/face/visible', 'POST', { visible: !on }).catch(() => {});
  if (on) {
    closeMenu();
    openChat(false);
    await view.disappear();
    if (away) SHELL?.setVisible(false);
  } else {
    SHELL?.setVisible(true);
    await view.appear();
  }
}
SHELL?.onShow(on => setAway(!on));
// in a browser the page stays, empty: a click brings the face back
document.addEventListener('click', () => { if (away && !SHELL) setAway(false); });

// --- right-click menu: quick things only; everything else is in the panel ------------------------------------------
function menuTemplate() {
  const m = status?.mode || {};
  const check = (id, label, checked) => ({ id, label, type: 'checkbox', checked: !!checked });
  const sep = { type: 'separator' };
  return [
    { id: 'stop', label: 'Stop', enabled: !!(status?.flags?.speaking || status?.flags?.thinking) },
    { id: 'chat', label: 'Type a message...' },
    { id: 'activity', label: 'Show activity' },
    ...(status?.backend?.type === 'claude' ? [{ id: 'transcript', label: 'Show full transcript' }] : []),
    { label: 'Tools', submenu: [{ id: 'tools:mcp', label: 'MCP servers...' }, { id: 'tools:skills', label: 'Skills...' }] },
    { id: 'new', label: 'New conversation' },
    ...(status?.backend?.capabilities?.includes('compact') ? [{ id: 'compact', label: 'Compact conversation' }] : []),
    sep,
    check('listening', 'Listening', m.listening),
    check('voice', 'Voice replies', m.voice),
    ...(SHELL ? [] : [check('captions', 'Captions', cfg.captions)]),
    sep,
    ...(status?.skip_permissions ? [{ id: 'skip:off', label: 'Stop skipping permissions' }] : []),
    { id: 'hide', label: 'Hide face' },
    { id: 'settings', label: 'Settings...' },
    { id: 'quit', label: 'Quit' },
  ];
}

async function action(id) {
  if (!id) return;
  const k = id.split(':')[0];
  const m = status?.mode || {};
  switch (k) {
    case 'stop': halt(); break;
    case 'chat': openChat(true); break;
    case 'activity': openPanel('activity#summary'); break;
    case 'transcript': openPanel('activity#transcript'); break;
    case 'tools': openPanel(`tools#${id.split(':')[1] || ''}`); break;          // the Tools tab's MCP or Skills list
    case 'new': newConversation(); break;
    case 'compact': compactConversation(); break;
    case 'listening': setMode({ listening: !m.listening }); break;
    case 'voice': setMode({ voice: !m.voice }); break;
    case 'captions': putFace({ captions: !cfg.captions }); break;
    case 'skip': setMode({ skip_permissions: false }); break;
    case 'hide': setAway(true); break;
    case 'settings': openPanel('settings'); break;
    case 'quit': quit(); break;
    default: break;
  }
}

// browser fallback: an HTML menu; submenus become labelled sections (long ones a row of chips)
function closeMenu() { document.querySelector('.hmenu')?.remove(); }
function htmlMenu(items, x, y) {
  closeMenu();
  const box = el('div', 'hmenu');
  box.setAttribute('role', 'menu');
  const add = (list, parent) => {
    for (const it of list) {
      if (it.type === 'separator') { parent.append(el('div', 'hsep')); continue; }
      if (it.submenu) {
        parent.append(el('div', 'hhead', it.label));
        const body = el('div', it.submenu.length > 6 ? 'hchips' : '');
        add(it.submenu.filter(s => !(body.className && s.type === 'separator')), body);
        parent.append(body);
        continue;
      }
      const row = el('button', `hitem ${it.checked ? 'checked' : ''}`, it.label);
      row.setAttribute('role', it.type === 'checkbox' || it.type === 'radio' ? 'menuitemcheckbox' : 'menuitem');
      if (it.type) row.setAttribute('aria-checked', String(!!it.checked));
      row.disabled = it.enabled === false;
      row.onclick = () => { closeMenu(); action(it.id); };
      parent.append(row);
    }
  };
  add(items, box);
  document.body.append(box);
  const r = box.getBoundingClientRect();
  box.style.left = Math.max(4, Math.min(x, innerWidth - r.width - 4)) + 'px';
  box.style.top = Math.max(4, Math.min(y, innerHeight - r.height - 4)) + 'px';
  box.querySelector('.hitem:not(:disabled)')?.focus({ preventScroll: true });
  setTimeout(() => document.addEventListener('pointerdown', e => { if (!box.contains(e.target)) box.remove(); }, { once: true }), 0);
}

document.addEventListener('contextmenu', async e => {
  e.preventDefault();
  if (e.target.closest('input')) return;
  if (SHELL) { interact(true); try { action(await SHELL.menu(menuTemplate())); } finally { interact(false); } }
  else htmlMenu(menuTemplate(), e.clientX, e.clientY);
});

// --- connection ------------------------------------------------------------------------------------------------
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(TOKEN)}`);
  // the backend's idea of whether the face is on screen follows the window (it may have restarted meanwhile)
  ws.onopen = () => { if (away) send('/api/face/visible', 'POST', { visible: false }).catch(() => {}); };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type !== 'face_config') view.message(msg);       // the look goes on through applyConfig (the skin sweeps)
    if (msg.type === 'status') onStatus(msg);
    else if (msg.type === 'face_config') applyConfig(msg.config);
    else if (msg.type === 'event') onEvent(msg.ev);
    else if (msg.type === 'say') say(msg.text);
    else if (msg.type === 'notice') notice(msg.text, null, 6000);
    else if (msg.type === 'face_drawn') mapDrawn(msg.id);
    else if (msg.type === 'meter') meter.push(msg);
  };
  ws.onclose = () => {
    showState(STATES.offline[0], 'reconnecting');
    view.status = null;
    setTimeout(connect, 1500);
  };
}

async function boot() {
  if (!TOKEN) { notice('Missing access token: start the face with Wireface.cmd.', null, 0); }
  if (SHELL) {
    const init = await SHELL.init();
    if (init?.mode) mode = init.mode;
    if (init?.visible === false) { away = true; document.body.classList.add('away'); view.setGone(); }   // reloaded while away
  }
  setBodyMode(mode);
  try {
    const d = await send('/api/face', 'GET');
    applyConfig(d.config);
  } catch (e) { notice(`Can't reach ${status?.agent || 'Claude'}: ${e.message}`, null, 0); }
  connect();
  view.start();
}
boot();
window.faceView = view;            // for debugging from devtools
