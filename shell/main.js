// Wireface: a desktop shell around the face page served by the local backend (wireface).
//   floating: a transparent, frameless window holding just the face (clicks pass through around it)
//   windowed: the same face inside a floating panel with captions and a toolbar
// plus a tray icon (show or hide the face, the activity panel, settings, quit).
// Started by wireface/app.py with --face-url, --face-state and --face-profile.
'use strict';
const { app, BrowserWindow, dialog, ipcMain, Menu, screen, shell, Tray } = require('electron');
const fs = require('node:fs');
const path = require('node:path');

const arg = name => (process.argv.find(a => a.startsWith(`--${name}=`)) || '').slice(name.length + 3);
const URL_ = arg('face-url');
const STATE = arg('face-state');
const PROFILE = arg('face-profile');
if (!URL_) { console.error('missing --face-url'); process.exit(2); }
const ORIGIN = new URL(URL_).origin;
if (PROFILE) app.setPath('userData', PROFILE);

const SIZES = { floating: { w: 280, h: 320, min: 140, max: 900 }, windowed: { w: 380, h: 500, min: 260, max: 1200 } };
const ICON = path.join(__dirname, 'icon.ico');
const NAME = 'Wireface';                     // window titles and the tray tooltip (not the face's name)
let win = null;
let mode = 'floating';
let state = {};
let dragTimer = null;
let cursorTimer = null;

function loadState() {
  try { state = JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { state = {}; }
}
let saveTimer = null;
function saveState() {
  if (!STATE) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try { fs.mkdirSync(path.dirname(STATE), { recursive: true }); fs.writeFileSync(STATE, JSON.stringify(state, null, 2)); }
    catch (e) { console.error('saving window state', e); }
  }, 400);
}

function clampToScreen(b) {
  const d = screen.getDisplayMatching(b).workArea;
  const w = Math.min(b.width, d.width), h = Math.min(b.height, d.height);
  return { x: Math.max(d.x, Math.min(b.x, d.x + d.width - w)), y: Math.max(d.y, Math.min(b.y, d.y + d.height - h)), width: w, height: h };
}

function defaultBounds(m) {
  const d = screen.getPrimaryDisplay().workArea, s = SIZES[m];
  return { x: d.x + d.width - s.w - 40, y: d.y + d.height - s.h - 40, width: s.w, height: s.h };
}

function remember() {
  if (!win) return;
  state[mode] = win.getBounds();
  state.mode = mode;
  saveState();
}

function applyMode(m) {
  if (!SIZES[m]) return;
  if (win && mode !== m) remember();
  mode = m;
  const b = clampToScreen(state[m] || defaultBounds(m));
  win.setBounds(b);
  win.setIgnoreMouseEvents(m === 'floating', { forward: true });
  state.mode = m;
  saveState();
}

function createWindow() {
  loadState();
  mode = SIZES[state.mode] ? state.mode : 'floating';
  const b = clampToScreen(state[mode] || defaultBounds(mode));
  win = new BrowserWindow({
    ...b, frame: false, transparent: true, backgroundColor: '#00000000', resizable: false, maximizable: false,
    fullscreenable: false, hasShadow: false, skipTaskbar: true, alwaysOnTop: true, show: false, title: NAME, icon: ICON,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true,
                      nodeIntegration: false, backgroundThrottling: false, spellcheck: false },
  });
  win.setAlwaysOnTop(true, 'floating');
  win.setIgnoreMouseEvents(mode === 'floating', { forward: true });
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(ORIGIN)) e.preventDefault(); });
  win.once('ready-to-show', () => { if (wantShown) win.showInactive(); });
  win.on('moved', remember);
  win.on('closed', () => { win = null; });
  win.webContents.on('render-process-gone', (_e, d) => { console.error('renderer gone', d.reason); setTimeout(() => win && win.reload(), 1500); });
  // the page's warnings and errors go to shell.log: a face that fails to draw is otherwise just an empty window
  win.webContents.on('console-message', (_e, level, message, line, source) => {
    if (level >= 2) console.error(`[face page] ${message} (${String(source || '').split('/').pop()}:${line})`);
  });
  win.webContents.on('did-fail-load', () => setTimeout(() => win && win.loadURL(URL_ + '&shell=electron'), 3000));
  win.loadURL(URL_ + '&shell=electron');
  // the cursor anywhere on screen, relative to the window (eyes follow it)
  let last = '';
  cursorTimer = setInterval(() => {
    if (!win || win.isDestroyed() || !win.isVisible()) return;
    const c = screen.getCursorScreenPoint(), wb = win.getBounds();
    const key = `${c.x - wb.x},${c.y - wb.y}`;
    if (key !== last) { last = key; win.webContents.send('face:cursor', { x: c.x - wb.x, y: c.y - wb.y }); }
  }, 33);
}

// The face stays above the panel. Both are always-on-top windows, and among those Windows puts the one activated last
// in front: whenever the panel comes forward the face is moved back on top (without taking the focus).
function faceOnTop() {
  if (win && !win.isDestroyed() && win.isVisible() && win.isAlwaysOnTop()) win.moveTop();
}
const faceBackOnTop = () => { faceOnTop(); setTimeout(faceOnTop, 60); };   // again once Windows has raised the panel

// --- the activity panel: a second, resizable window holding the agent's log and the settings ---------------------
const TOKEN = new URLSearchParams(new URL(URL_).hash.slice(1)).get('t') || '';
const PANEL = { w: 480, h: 640, minW: 380, minH: 300 };
// docked as a drawer: full height against a screen edge, as wide as the user drags it, or folded into a tab
const DRAWER = { w: 440, minW: 320, maxW: 900, tab: 34 };
let panel = null;
let panelSave = null;
let drawerAnim = null;
let snapping = false;
const panelAlive = () => panel && !panel.isDestroyed();
const docked = () => state.panelDock === 'left' || state.panelDock === 'right';

function drawerBounds(collapsed = !!state.panelCollapsed) {
  const near = panelAlive() ? panel.getBounds() : (win ? win.getBounds() : null);
  const d = (near ? screen.getDisplayMatching(near) : screen.getPrimaryDisplay()).workArea;
  const width = collapsed ? DRAWER.tab : Math.max(DRAWER.minW, Math.min(DRAWER.maxW, state.panelDockWidth || DRAWER.w, d.width));
  return { x: state.panelDock === 'right' ? d.x + d.width - width : d.x, y: d.y, width, height: d.height };
}

/** Move the panel to where it belongs (the drawer, or its own floating place), sliding when it is the drawer. */
function placePanel(animate = true) {
  if (!panelAlive()) return;
  clearInterval(drawerAnim);
  const collapsed = docked() && !!state.panelCollapsed;
  panel.setMinimumSize(collapsed ? DRAWER.tab : (docked() ? DRAWER.minW : PANEL.minW), PANEL.minH);
  panel.setResizable(!collapsed);
  panel.setMovable(!docked());
  if (panel.isMaximized()) panel.unmaximize();
  const to = docked() ? drawerBounds() : clampToScreen(state.panel || panelBounds());
  const from = panel.getBounds();
  const steps = animate && docked() ? 8 : 1;
  let i = 0;
  snapping = true;
  const step = () => {
    i++;
    const k = 1 - Math.pow(1 - i / steps, 3);                 // ease out
    const mix = (a, b) => Math.round(a + (b - a) * k);
    panel.setBounds({ x: mix(from.x, to.x), y: mix(from.y, to.y), width: mix(from.width, to.width), height: mix(from.height, to.height) });
    if (i >= steps) { clearInterval(drawerAnim); drawerAnim = null; setTimeout(() => { snapping = false; }, 50); }
  };
  if (steps === 1) step(); else drawerAnim = setInterval(step, 16);
  faceOnTop();
  panel.webContents.send('panel:dock', { dock: state.panelDock || null, collapsed });
}

function panelBounds() {
  const d = screen.getPrimaryDisplay().workArea;
  const b = win ? win.getBounds() : { x: d.x + d.width - 300, y: d.y + d.height - 340, width: 260, height: 300 };
  // beside the face, on whichever side has room
  const left = b.x - PANEL.w - 12;
  return { x: left >= d.x ? left : Math.min(b.x + b.width + 12, d.x + d.width - PANEL.w), y: Math.max(d.y, Math.min(b.y, d.y + d.height - PANEL.h)), width: PANEL.w, height: PANEL.h };
}

function openPanel(tab, toggle) {
  if (panelAlive()) {
    // a drawer folds and unfolds; a floating panel closes if it is in plain view (focused, or kept on top)
    if (docked() && panel.isVisible() && !panel.isMinimized()) {
      if (toggle || state.panelCollapsed) {
        state.panelCollapsed = toggle ? !state.panelCollapsed : false;
        saveState();
        placePanel();
      }
      if (tab) panel.webContents.send('panel:tab', tab);
      return;
    }
    if (toggle && panel.isVisible() && !panel.isMinimized() && (panel.isFocused() || panel.isAlwaysOnTop())) { panel.close(); return; }
    if (panel.isMinimized()) panel.restore();
    panel.show(); panel.focus();
    if (tab) panel.webContents.send('panel:tab', tab);
    return;
  }
  if (docked()) state.panelCollapsed = false;                    // opened on purpose: show it
  const b = docked() ? drawerBounds(false) : clampToScreen(state.panel || panelBounds());
  panel = new BrowserWindow({
    ...b, minWidth: PANEL.minW, minHeight: PANEL.minH, frame: false, resizable: true, maximizable: true, fullscreenable: false,
    backgroundColor: '#0f1218', show: false, skipTaskbar: false, title: NAME, icon: ICON,
    alwaysOnTop: state.panelPinned !== false,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true,
                      nodeIntegration: false, spellcheck: false },
  });
  if (state.panelPinned !== false) panel.setAlwaysOnTop(true, 'floating');
  panel.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  panel.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(ORIGIN)) e.preventDefault(); });
  panel.once('ready-to-show', () => { panel.show(); if (docked()) placePanel(false); });
  const remember = () => {
    if (!panelAlive() || panel.isMaximized() || panel.isMinimized() || snapping) return;
    if (docked()) {
      // the drawer keeps its edge and full height; only the width the user dragged to is kept
      if (!state.panelCollapsed) state.panelDockWidth = panel.getBounds().width;
      placePanel(false);
    } else {
      state.panel = panel.getBounds();
    }
    clearTimeout(panelSave);
    panelSave = setTimeout(saveState, 300);
  };
  panel.on('moved', remember);
  panel.on('resized', remember);
  for (const ev of ['focus', 'show', 'restore', 'always-on-top-changed']) panel.on(ev, faceBackOnTop);
  panel.on('closed', () => { panel = null; });
  panel.webContents.on('did-fail-load', () => setTimeout(() => panelAlive() && panel.loadURL(panelUrl(tab)), 3000));
  panel.loadURL(panelUrl(tab));
}
const panelUrl = tab => `${ORIGIN}/panel#t=${encodeURIComponent(TOKEN)}&shell=electron&tab=${encodeURIComponent(tab || 'activity')}`;
const fromPanel = e => panelAlive() && e.sender === panel.webContents;

// --- the tray icon: click to show or hide the face; right-click for the menu ---------------------------------------
let tray = null;
let wantShown = true;            // what the face was last asked to be (going away takes the page a moment to animate)
let showTimer = null;
const faceShown = () => !!win && wantShown && win.isVisible();

/** Ask the page to send the face away (it animates, then hides the window) or bring it back. */
function setFaceShown(on) {
  if (!win) return;
  wantShown = on;
  win.webContents.send('face:show', on);
  // a page that doesn't answer (still loading, or broken) must not leave the face stuck either way
  clearTimeout(showTimer);
  showTimer = setTimeout(() => { if (win && win.isVisible() !== on) (on ? win.showInactive() : win.hide()); }, on ? 1500 : 3000);
}

function trayMenu() {
  const shown = faceShown();
  return Menu.buildFromTemplate([
    { label: shown ? 'Hide face' : 'Show face', click: () => setFaceShown(!shown) },
    { label: 'Show activity', click: () => openPanel('activity#summary') },     // its Raw view: the face's menu
    { label: 'Settings...', click: () => openPanel('settings') },
    { type: 'separator' },
    { label: 'Quit', click: () => { remember(); app.quit(); } },
  ]);
}

function createTray() {
  tray = new Tray(ICON);
  tray.setToolTip(NAME);
  tray.on('click', () => setFaceShown(!faceShown()));
  tray.on('right-click', () => tray.popUpContextMenu(trayMenu()));     // built fresh: the label says what it will do
}

// --- requests from the page ----------------------------------------------------------------------------
const fromPage = e => win && e.sender === win.webContents;

ipcMain.handle('face:init', e => fromPage(e) ? { mode, visible: wantShown } : null);
ipcMain.on('face:mode', (e, m) => { if (fromPage(e)) applyMode(m); });
ipcMain.on('face:ignore-mouse', (e, ignore) => { if (fromPage(e)) win.setIgnoreMouseEvents(!!ignore, { forward: true }); });
ipcMain.on('face:on-top', (e, on) => { if (fromPage(e)) { win.setAlwaysOnTop(!!on, 'floating'); faceOnTop(); } });
ipcMain.on('face:visible', (e, on) => {
  if (!fromPage(e)) return;
  wantShown = !!on;
  clearTimeout(showTimer);
  if (on && !win.isVisible()) { win.showInactive(); faceOnTop(); }
  else if (!on && win.isVisible()) win.hide();
});
ipcMain.on('face:focus', e => { if (fromPage(e)) { win.setIgnoreMouseEvents(false); win.focus(); } });
ipcMain.on('face:quit', e => { if (fromPage(e)) { remember(); setTimeout(() => app.quit(), 100); } });

function follow(apply) {
  clearInterval(dragTimer);
  const start = screen.getCursorScreenPoint(), b0 = win.getBounds();
  dragTimer = setInterval(() => {
    if (!win || win.isDestroyed()) return clearInterval(dragTimer);
    const c = screen.getCursorScreenPoint();
    apply(c.x - start.x, c.y - start.y, b0);
  }, 12);
}
ipcMain.on('face:drag', (e, on) => {
  if (!fromPage(e)) return;
  if (!on) { clearInterval(dragTimer); dragTimer = null; remember(); return; }
  follow((dx, dy, b0) => win.setBounds({ x: b0.x + dx, y: b0.y + dy, width: b0.width, height: b0.height }));
});
ipcMain.on('face:resize', (e, on) => {
  if (!fromPage(e)) return;
  if (!on) { clearInterval(dragTimer); dragTimer = null; remember(); return; }
  const s = SIZES[mode];
  follow((dx, dy, b0) => win.setBounds({
    x: b0.x, y: b0.y, width: Math.max(s.min, Math.min(s.max, b0.width + dx)),
    height: Math.max(s.min * 1.15, Math.min(s.max * 1.3, b0.height + dy)) | 0 }));
});
// zoom: grow/shrink around the window's centre
ipcMain.on('face:zoom', (e, factor, absWidth) => {
  if (!fromPage(e)) return;
  const b = win.getBounds(), s = SIZES[mode];
  const w = Math.round(Math.max(s.min, Math.min(s.max, absWidth || b.width * factor)));
  const h = Math.round(w * (b.height / b.width));
  const cx = b.x + b.width / 2, cy = b.y + b.height / 2;
  win.setBounds(clampToScreen({ x: Math.round(cx - w / 2), y: Math.round(cy - h / 2), width: w, height: h }));
  remember();
});
// native context menu: the page sends a template, gets back the id of the chosen item. On Windows the menu reports
// that it closed before the click on an item in a submenu arrives, so the choice is answered from the click itself and
// "nothing chosen" only after a grace period (resolving at once lost every View, Size and Face choice).
ipcMain.handle('face:menu', (e, template) => new Promise(resolve => {
  if (!fromPage(e)) return resolve(null);
  let done = false;
  const finish = id => { if (!done) { done = true; resolve(id); } };
  const build = items => items.map(it => {
    if (it.type === 'separator') return { type: 'separator' };
    const out = { label: it.label, type: it.type || 'normal', checked: !!it.checked, enabled: it.enabled !== false };
    if (it.accelerator) out.accelerator = it.accelerator;
    if (it.submenu) out.submenu = build(it.submenu);
    else out.click = () => finish(it.id);
    return out;
  });
  Menu.buildFromTemplate(build(template)).popup({ window: win, callback: () => setTimeout(() => finish(null), 400) });
}));
ipcMain.on('face:panel', (e, tab, toggle) => { if (fromPage(e)) openPanel(tab, !!toggle); });
ipcMain.on('panel:pin', (e, on) => {
  if (!fromPanel(e)) return;
  state.panelPinned = !!on;
  panel.setAlwaysOnTop(!!on, 'floating');
  saveState();
});
ipcMain.on('panel:close', e => { if (fromPanel(e)) panel.close(); });
ipcMain.on('panel:minimize', e => { if (fromPanel(e)) panel.minimize(); });
ipcMain.on('panel:maximize', e => { if (fromPanel(e)) (panel.isMaximized() ? panel.unmaximize() : panel.maximize()); });
ipcMain.handle('panel:init', e => fromPanel(e) ? { pinned: state.panelPinned !== false, dock: state.panelDock || null,
  collapsed: docked() && !!state.panelCollapsed } : null);
// dock the panel to a screen edge as a drawer ('left' / 'right'), or let it float again (null)
ipcMain.on('panel:dock', (e, side) => {
  if (!fromPanel(e)) return;
  if (!docked() && !panel.isMaximized()) state.panel = panel.getBounds();      // where it floats, for later
  state.panelDock = side === 'left' || side === 'right' ? side : null;
  state.panelCollapsed = false;
  if (state.panelDock && state.panelPinned === false) {          // a drawer for keeping an eye on things stays on top
    state.panelPinned = true;
    panel.setAlwaysOnTop(true, 'floating');
  }
  saveState();
  placePanel();
});
// fold the drawer into a slim tab at the screen edge, or slide it out again
ipcMain.on('panel:collapse', (e, on) => {
  if (!fromPanel(e) || !docked()) return;
  state.panelCollapsed = !!on;
  saveState();
  placePanel();
});
ipcMain.handle('panel:choose-folder', async (e, current) => {
  if (!fromPanel(e)) return null;
  const r = await dialog.showOpenDialog(panel, { title: 'Working folder', defaultPath: current || undefined, properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0] || null;
});
// folder picker (the working folder Claude Code runs in): resolves to the chosen path or null
ipcMain.handle('face:choose-folder', async (e, current) => {
  if (!fromPage(e)) return null;
  const r = await dialog.showOpenDialog(win, { title: 'Working folder', defaultPath: current || undefined, properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0] || null;
});
ipcMain.on('face:open-external', (e, url) => { if ((fromPage(e) || fromPanel(e)) && /^https?:\/\//.test(url)) shell.openExternal(url); });

// --- lifecycle -------------------------------------------------------------------------------------------------
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => setFaceShown(true));
  app.whenReady().then(() => {
    createWindow();
    createTray();
    screen.on('display-removed', () => win && win.setBounds(clampToScreen(win.getBounds())));
    // the work area changed (taskbar, resolution, a screen added or removed): the drawer follows its edge
    for (const ev of ['display-metrics-changed', 'display-added', 'display-removed']) screen.on(ev, () => docked() && placePanel(false));
  });
  app.on('window-all-closed', () => app.quit());
  // the GPU process restarts after a crash or driver reset; reload the page so the face redraws
  app.on('child-process-gone', (_e, d) => {
    if (d.type === 'GPU' && win) { console.error('GPU process gone:', d.reason); setTimeout(() => win && win.reload(), 1500); }
  });
  app.on('before-quit', () => { clearInterval(cursorTimer); tray?.destroy(); remember(); if (panelAlive() && !docked() && !panel.isMaximized() && !panel.isMinimized()) { state.panel = panel.getBounds(); saveState(); } });
}
