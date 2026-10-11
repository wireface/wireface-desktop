// The Tools tab: the agent's MCP servers and skills, as its own configuration has them (wireface/harness_tools.py),
// each with a switch that changes that configuration the agent's own way, and recommended ones to install.
//   the agent (where it works, when it was checked, refresh)        stays at the top
//   a restart waiting, problems reading its files                    when there are any
//   MCP servers | Skills                                             sub-tabs ("tools#mcp", "tools#skills")
//   search ( / ), Installed | Recommended | All, Any | On | Off | Problems
//   the list: installed ones grouped by where they come from (rows with a switch and details behind a disclosure), or
//   the recommended ones as cards that say what Install will do
// Statuses are checked in the background (the agent's own health check) and arrive over the WebSocket ("tools");
// nothing here waits for them. Names, paths and commands come from config files: put in the page as text only.
import { $, h, send, SHELL } from './common.js';
import { toggle, btn, icon, ICONS, keepFocus } from './ui.js';

let view = null, failed = null, loading = false;
let toast = () => {}, openTab = () => {};
let lastSig = '';
const busy = new Set();           // switches and installs on their way to the server
const opened = new Set();         // rows whose details are open
let flash = null;                 // a row to bring into view and highlight ("Show it")

// --- remembered: the sub-tab, and each one's filter --------------------------------------------------------------------
const SUBS = { mcp: 'MCP servers', skills: 'Skills' };
const store = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* fine */ } };
const stored = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } };
let sub = SUBS[stored('panel.tools.sub', 'mcp')] ? stored('panel.tools.sub', 'mcp') : 'mcp';
const filters = Object.fromEntries(Object.keys(SUBS).map(k => [k, { show: 'installed', state: 'any', ...stored(`panel.tools.filter.${k}`, {}) }]));
const queries = { mcp: '', skills: '' };

const root = () => $('tab-tools');
const visible = () => !!root() && !root().hidden;
const F = () => filters[sub];
const Q = () => queries[sub].trim().toLowerCase();

const ICON = {
  wrench: 'M14.6 4.2a4.6 4.6 0 0 0-4.9 6.2l-5.3 5.3a1.9 1.9 0 0 0 2.7 2.7l5.3-5.3a4.6 4.6 0 0 0 6.2-4.9l-2.6 2.6-2.4-.6-.6-2.4z',
  plug: 'M9 3.5v4M15 3.5v4|M6.5 7.5h11v3.5a5.5 5.5 0 0 1-11 0z|M12 16.5v4',
  spark: 'M12 3.5l1.8 5.2 5.2 1.8-5.2 1.8L12 17.5l-1.8-5.2L5 10.5l5.2-1.8z',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1|M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  search: 'M11 4.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 0 0 0-13z|M20 20l-4.2-4.2',
  lock: 'M7 10.5h10v9H7z|M9 10.5V8a3 3 0 0 1 6 0v2.5',
  x: 'M7 7l10 10M17 7L7 17',
  arrow: 'M5 12h13M13 7l5 5-5 5',
  chev: 'M9.5 6.5l5.5 5.5-5.5 5.5',
};
const GROUPS = [
  ['wireface', 'Built into Wireface', 'The face gives these to the agent'],
  ['local', 'This folder', 'Yours, only for this working folder'],
  ['project', 'Project', 'In the project\'s own config, shared with it'],
  ['user', 'Yours', 'Yours, in every folder'],
  ['plugin', 'Plugins', 'Come with a plugin'],
  ['claude.ai', 'claude.ai', 'From your claude.ai account'],
  ['system', 'Built into the agent', 'Come with the agent itself'],
  ['managed', 'Managed', 'Set by your organisation'],
  ['other', 'Other', 'Known to the agent only'],
];
const groupOf = scope => (scope === 'global' ? 'user' : GROUPS.some(g => g[0] === scope) ? scope : 'other');
const BADGE = { wireface: 'Wireface', local: 'This folder', project: 'Project', user: 'Yours', global: 'Yours', plugin: 'Plugin',
  'claude.ai': 'claude.ai', system: 'Built in', managed: 'Managed', other: 'Other' };
const STATUS = { connected: 'Connected', failed: 'Failed', 'needs-auth': 'Needs sign-in', pending: 'Connecting', disabled: 'Off',
  unknown: 'Not checked', builtin: 'Built in' };

// --- feeding it -------------------------------------------------------------------------------------------------------------
export function init(hooks) {
  ({ toast, openTab } = hooks);
  build();
  document.addEventListener('keydown', e => {             // "/" finds, as in most lists
    if (e.key !== '/' || !visible() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
    e.preventDefault();
    $('tl-q').focus();
  });
}

/** The tab came into view ("tools#skills" picks the sub-tab; "-recommended" after it, that view): the latest list, and
 *  a background check if the last one is old. */
export function shown(anchor) {
  const [want, show] = String(anchor || '').split('-');
  if (SUBS[want]) setSub(want, false);
  if (show === 'recommended' || show === 'installed' || show === 'all') setFilter({ show }, false);
  render();
  load(true);
}

export function update(v) {
  if (!v) return;
  view = v;
  failed = null;
  if (visible()) render();
}

/** The agent, its folder or whether it runs changed: read the list again. */
export function status(s) {
  const sig = JSON.stringify([s.backend?.type, s.cwd, s.beacon?.state === 'offline']);
  if (sig === lastSig) return;
  const first = !lastSig;
  lastSig = sig;
  if (first) return;
  if (visible()) load(false);
  else view = null;
}

async function load(check) {
  if (loading) return;
  loading = true;
  try {
    view = (await send(`/api/tools${check ? '?check=1' : ''}`)).view;
    failed = null;
  } catch (e) {
    failed = e.message;
  }
  loading = false;
  if (visible()) render();
}

// --- actions ------------------------------------------------------------------------------------------------------------
async function act(key, path, method, body, after) {
  busy.add(key);
  render();
  try {
    const r = await send(path, method, body);
    if (r.view) view = r.view;
    if (r.message) toast(r.message);
    after?.();
  } catch (e) {
    toast(e.message, 8000);
  }
  busy.delete(key);
  render();
}

const setServer = (s, on) => act(`s:${s.key}`, '/api/tools/server', 'PUT', { key: s.key, enabled: on });
const setSkill = (s, on) => act(`k:${s.key}`, '/api/tools/skill', 'PUT', { key: s.key, enabled: on });
const install = item => act(`i:${item.id}`, '/api/tools/install', 'POST', { id: item.id });
const restart = () => act('restart', '/api/tools/restart', 'POST', {});
const refresh = () => act('refresh', '/api/tools/refresh', 'POST', {});

function setSub(name, draw = true) {
  sub = name;
  store('panel.tools.sub', name);
  if (draw) { render(); $('tl-body').scrollTop = 0; }
}

function setFilter(patch, draw = true) {
  Object.assign(F(), patch);
  store(`panel.tools.filter.${sub}`, { show: F().show, state: F().state });
  if (draw) { render(); $('tl-body').scrollTop = 0; }
}

function setQuery(q, draw = true) {
  queries[sub] = q;
  if ($('tl-q').value !== q) $('tl-q').value = q;
  $('tl-qx').hidden = !q;
  if (draw) renderBody();
}

/** Jump from a recommendation to the item it installed: the Installed list, everything showing, the row open. */
function showIt(key) {
  setQuery('', false);
  setFilter({ show: 'installed', state: 'any' }, false);
  opened.add(key);
  flash = key;
  render();
}

function copy(text, what) {
  navigator.clipboard?.writeText(text).then(() => toast(`${what} copied.`), () => toast('Couldn\'t copy it.'));
}

function external(href, label) {
  return h('a', { class: 'tl-link', href, target: '_blank', rel: 'noopener', title: href,
    onclick: e => { if (SHELL?.openExternal) { e.preventDefault(); SHELL.openExternal(href); } } }, label, icon(ICON.arrow));
}

// --- what is in each list -----------------------------------------------------------------------------------------------
const installedOf = s => (s === 'mcp' ? view?.servers : view?.skills) || [];
const recommendedOf = s => (view?.recommended || []).filter(r => r.kind === (s === 'mcp' ? 'mcp' : 'skill'));
const problem = it => sub === 'mcp' && it.enabled && (it.status === 'failed' || it.status === 'needs-auth' || !!it.missing);
const problemsIn = list => list.filter(it => it.enabled && (it.status === 'failed' || it.status === 'needs-auth' || !!it.missing)).length;
const locked = it => !it.can_toggle && !it.setting;

function words(it) {
  return [it.name, it.description, BADGE[it.scope], it.source, it.target, it.detail, it.missing, it.path, it.why,
    ...(it.env || []), ...(it.tool_names || [])].filter(Boolean).join(' ').toLowerCase();
}
const cardWords = r => [r.title, r.id, r.description, r.why, r.by, r.auth, r.plan?.command, ...(r.needs || []).map(n => n.label)]
  .filter(Boolean).join(' ').toLowerCase();

function stateOk(it) {
  const st = F().state;
  return st === 'any' || (st === 'on' && it.enabled) || (st === 'off' && !it.enabled) || (st === 'problems' && problem(it));
}

/** A path (or a command line holding one) with the home folder shown as ~. */
function tidy(text) {
  const home = view?.home;
  if (!home || !text) return text || '';
  const pattern = home.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\\\\|\//g, '[\\\\/]');
  return String(text).replace(new RegExp(pattern, 'gi'), '~');
}

/** Text with the search's matches marked (as text nodes and <mark>s: never HTML). */
function hl(text) {
  const t = String(text ?? ''), q = Q();
  if (!q || !t) return t;
  const out = [], low = t.toLowerCase();
  let i = 0;
  for (let at = low.indexOf(q); at >= 0; at = low.indexOf(q, i)) {
    if (at > i) out.push(t.slice(i, at));
    out.push(h('mark', { class: 'tl-hit' }, t.slice(at, at + q.length)));
    i = at + q.length;
  }
  out.push(t.slice(i));
  return out;
}

// --- the frame: built once, so the search box keeps its focus while the rest redraws -----------------------------------
function build() {
  const q = h('input', { id: 'tl-q', type: 'search', placeholder: 'Search', autocomplete: 'off', spellcheck: 'false',
    'aria-label': 'Search name, description, source and command (press / to find)',
    oninput: e => setQuery(e.target.value),
    onkeydown: e => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      if (e.target.value) setQuery(''); else e.target.blur();
    } });
  root().replaceChildren(h('div', { class: 'tl-frame' },
    h('div', { class: 'tl-fixed' },
      h('div', { class: 'tl-in' },
        h('div', { id: 'tl-head', class: 'tl-head' }),
        h('div', { id: 'tl-banner' }),
        h('div', { id: 'tl-subs', class: 'tl-subs', role: 'tablist', 'aria-label': 'Tools' }),
        h('div', { class: 'tl-toolbar' },
          h('label', { class: 'searchbox tl-search' }, icon(ICON.search), q,
            h('kbd', { class: 'tl-kbd', 'aria-hidden': 'true' }, '/'),
            h('button', { id: 'tl-qx', class: 'tl-qx', hidden: true, title: 'Clear the search (Esc)', 'aria-label': 'Clear the search',
              onclick: e => { e.preventDefault(); setQuery(''); q.focus(); } }, icon(ICON.x))),
          h('div', { id: 'tl-filters', class: 'tl-filters' })),
        h('div', { id: 'tl-count', class: 'tl-count', 'aria-live': 'polite' }))),
    h('div', { id: 'tl-body', class: 'tl-body', role: 'tabpanel', tabindex: '-1' })));
}

function render() {
  if (!$('tl-head')) return;
  for (const [id, draw] of [['tl-head', head], ['tl-banner', banner], ['tl-subs', subtabs], ['tl-filters', filterBar]]) {
    const el = $(id);
    keepFocus(el, () => el.replaceChildren(...[draw()].flat().filter(Boolean)));
  }
  const q = $('tl-q');
  if (q.value !== queries[sub]) q.value = queries[sub];
  q.placeholder = sub === 'mcp' ? 'Search servers' : 'Search skills';
  $('tl-qx').hidden = !queries[sub];
  renderBody();
}

// --- the agent ----------------------------------------------------------------------------------------------------------
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

function head() {
  if (!view?.harness) {
    return h('div', { class: 'tl-agent' }, h('span', { class: 'tl-agent-ico' }, icon(ICON.wrench)),
      h('div', {}, h('span', { class: 'sk sk-title' }), h('span', { class: 'sk sk-line' })));
  }
  const c = view.check || {};
  const checking = c.state === 'running' || busy.has('refresh');
  const folder = (view.cwd || '').split(/[\\/]/).filter(Boolean).pop() || view.cwd;
  const live = c.source === 'the running session';
  const said = checking ? 'Checking...' : c.at ? `Checked ${ago(c.at)}` : 'Not checked yet';
  const from = !view.harness.supported ? '' : view.running ? (live ? 'from its session' : '') : 'not running';
  return [
    h('div', { class: 'tl-agent' }, h('span', { class: 'tl-agent-ico' }, icon(ICON.wrench)),
      h('div', { class: 'tl-agent-text' }, h('strong', {}, view.harness.label),
        h('span', { class: 'tl-where', title: view.cwd }, `in ${folder}`))),
    h('span', { class: 'spacer' }),
    view.harness.supported ? h('span', { class: `tl-checked ${view.running ? '' : 'off'}`, title: c.error || [said, c.source && `(${c.source})`].filter(Boolean).join(' ') },
      checking ? h('span', { class: 'spin' }) : h('span', { class: `tl-live ${view.running ? 'on' : ''}` }),
      h('span', {}, said), from ? h('span', { class: 'tl-from' }, from) : null) : null,
    view.harness.supported ? h('button', { class: 'iconbtn tl-refresh', title: 'Check every server again (it can take a while)', 'aria-label': 'Check again',
      disabled: checking || null, dataset: { k: 'tl-refresh' }, onclick: refresh }, icon(ICONS.refresh)) : null,
  ];
}

function banner() {
  if (!view?.harness) return null;
  const c = view.check || {};
  return [
    view.pending ? h('div', { class: 'callout busy tl-pending' }, icon(ICONS.info), h('div', {}, view.pending),
      view.restart?.can ? btn(busy.has('restart') ? 'Restarting...' : 'Restart now', restart, { kind: 'primary sm', disabled: busy.has('restart') }) : null) : null,
    c.error ? h('div', { class: 'callout bad' }, icon(ICONS.alert),
      h('div', {}, `Couldn't check the servers: ${c.error}. Is ${view.harness.label} installed and signed in?`),
      btn('Retry', refresh, { kind: 'sm' })) : null,
    ...(view.errors || []).map(e => h('div', { class: 'callout bad' }, icon(ICONS.alert), h('div', {}, e))),
  ];
}

// --- MCP servers | Skills ----------------------------------------------------------------------------------------------
function subtabs() {
  const tabs = Object.keys(SUBS);
  return tabs.map(name => {
    const n = view?.harness?.supported ? installedOf(name).length : null;
    const bad = name === 'mcp' && view ? problemsIn(installedOf('mcp')) : 0;
    const on = name === sub;
    return h('button', { role: 'tab', class: on ? 'on' : '', 'aria-selected': String(on), tabindex: on ? '0' : '-1',
      'aria-controls': 'tl-body', dataset: { k: `tl-sub:${name}` }, id: `tl-sub-${name}`,
      onclick: () => setSub(name),
      onkeydown: e => {
        const i = tabs.indexOf(name);
        const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
        if (to === undefined) return;
        e.preventDefault();
        setSub(tabs[(to + tabs.length) % tabs.length]);
        $(`tl-sub-${sub}`)?.focus();
      } },
    icon(name === 'mcp' ? ICON.plug : ICON.spark), h('span', { class: 'tl-sublabel' }, SUBS[name]),
    n !== null ? h('span', { class: 'tl-n' }, String(n)) : null,
    bad ? h('span', { class: 'tl-bad', title: `${bad} with a problem` }, String(bad)) : null);
  });
}

// --- the filters ------------------------------------------------------------------------------------------------------
function filterBar() {
  if (!view?.harness?.supported) return null;
  const f = F(), inst = installedOf(sub), recs = recommendedOf(sub);
  const show = [['installed', 'Installed', inst.length], ['recommended', 'Recommended', recs.length], ['all', 'All', null]];
  const states = [['any', 'Any'], ['on', 'On'], ['off', 'Off'], ...(sub === 'mcp' ? [['problems', 'Problems']] : [])];
  const count = st => (st === 'problems' ? problemsIn(inst) : st === 'on' ? inst.filter(i => i.enabled).length
    : st === 'off' ? inst.filter(i => !i.enabled).length : inst.length);
  return [
    h('div', { class: 'seg tl-show', role: 'group', 'aria-label': 'Show' },
      show.map(([v, label, n]) => h('button', { class: f.show === v ? 'on' : '', 'aria-pressed': String(f.show === v),
        dataset: { k: `tl-show:${v}` }, onclick: () => setFilter({ show: v }) }, label, n !== null ? h('span', { class: 'tl-n' }, String(n)) : null))),
    f.show !== 'recommended' ? h('div', { class: 'tl-states', role: 'group', 'aria-label': 'State' },
      states.map(([v, label]) => {
        const n = count(v);
        return h('button', { class: `chipbtn tl-state ${f.state === v ? 'on' : ''} ${v === 'problems' && n ? 'bad' : ''}`,
          'aria-pressed': String(f.state === v), dataset: { k: `tl-state:${v}` }, onclick: () => setFilter({ state: v }) },
        label, v !== 'any' ? h('span', { class: 'tl-n' }, String(n)) : null);
      })) : null,
  ];
}

// --- the list -------------------------------------------------------------------------------------------------------------
function renderBody() {
  const body = $('tl-body'), countEl = $('tl-count');
  if (!body) return;
  let shown = null, total = null;
  keepFocus(body, () => {
    if (failed && !view) {
      body.replaceChildren(empty(ICONS.alert, 'Can\'t read the agent\'s tools', failed, ['Try again', () => load(true)]));
      return;
    }
    if (!view) {
      body.replaceChildren(skeleton());
      return;
    }
    if (!view.harness?.supported) {
      body.replaceChildren(empty(ICON.wrench, `Nothing to show for ${view.harness?.label || 'this agent'}`,
        'The Tools tab knows Claude Code, Codex and OpenCode. Choose one in the Agent tab.', ['Open the Agent tab', () => openTab('agent')]));
      return;
    }
    const f = F(), q = Q();
    const inst = installedOf(sub), recs = recommendedOf(sub);
    const rows = inst.filter(it => stateOk(it) && (!q || words(it).includes(q)));
    const cards = recs.filter(r => (!q || cardWords(r).includes(q)) && (f.show === 'recommended' || !r.installed));
    const parts = [];
    if (f.show !== 'recommended') {
      parts.push(...(rows.length ? groups(rows) : [noRows(inst, rows)]));
      shown = rows.length;
      total = inst.length;
    }
    if (f.show !== 'installed') {
      const head = f.show === 'all' ? h('div', { class: 'tl-gh tl-gh-rec' }, h('span', {}, 'Recommended, not installed'),
        h('span', { class: 'tl-n' }, String(cards.length))) : null;
      if (cards.length) parts.push(head, h('div', { class: 'tl-cards' }, cards.map(card)));
      else if (f.show === 'recommended' || q) parts.push(head, q ? empty(ICONS.info, `No recommendations match "${queries[sub].trim()}"`, null, ['Clear search', () => setQuery('')], 'sm')
        : empty(ICONS.check, 'Everything recommended is installed', null, null, 'sm'));
      if (f.show === 'recommended') { shown = cards.length; total = recs.length; }
    }
    body.replaceChildren(h('div', { class: 'tl-in' }, h('div', { class: `tl-list ${busy.size ? 'busy' : ''}` }, parts.filter(Boolean)),
      f.show !== 'recommended' && rows.length ? h('p', { class: 'tl-note' }, view.notes?.[sub === 'mcp' ? 'servers' : 'skills'] || '') : null));
  });
  // "Showing x of y" while something narrows the list
  const narrowed = !!Q() || (F().show !== 'recommended' && F().state !== 'any');
  countEl.replaceChildren(...(narrowed && shown !== null ? [
    h('span', {}, `Showing ${shown} of ${total}`),
    h('button', { class: 'linkbtn', dataset: { k: 'tl-clear' }, onclick: () => { setQuery('', false); setFilter({ state: 'any' }); } }, icon(ICON.x), 'Clear filters'),
  ] : []));
  countEl.hidden = !countEl.childElementCount;
  if (flash) {
    const el = document.getElementById(rowId(flash));
    flash = null;
    if (el) {
      el.scrollIntoView({ block: 'center' });
      el.classList.add('flash');
      el.querySelector('.tl-rhead')?.focus({ preventScroll: true });
      setTimeout(() => el.classList.remove('flash'), 1400);
    }
  }
}

function skeleton() {
  return h('div', { class: 'tl-in tl-skeleton', 'aria-label': 'Reading the agent\'s tools', role: 'status' },
    h('span', { class: 'sk sk-gh' }),
    ...[0, 1, 2, 3, 4].map(i => h('div', { class: 'tl-r sk-row' },
      h('div', { class: 'sk-main' }, h('span', { class: 'sk sk-name', style: `width:${[38, 52, 30, 46, 40][i]}%` }), h('span', { class: 'sk sk-line' })),
      h('span', { class: 'sk sk-switch' }))));
}

function empty(ico, title, text, action, size = '') {
  return h('div', { class: `empty tl-empty ${size}` }, icon(ico), h('strong', {}, title), text ? h('span', {}, text) : null,
    action ? h('button', { class: 'btn sm', onclick: action[1] }, action[0]) : null);
}

/** Why the installed list came out empty, and the way on. */
function noRows(all) {
  const q = queries[sub].trim(), st = F().state, kind = sub === 'mcp' ? 'MCP servers' : 'skills';
  if (!all.length) {
    const add = { claude: 'claude mcp add', codex: 'codex mcp add', opencode: 'opencode mcp add' }[view.harness.type];
    return empty(sub === 'mcp' ? ICON.plug : ICON.spark, `${view.harness.label} has no ${kind} yet`,
      sub === 'mcp' ? `Install a recommended one, or add your own with "${add}".` : 'Install one of the recommended ones: Wireface copies it in.',
      ['Browse recommended', () => setFilter({ show: 'recommended' })]);
  }
  if (q) return empty(ICONS.info, `No ${kind} match "${q}"`, st !== 'any' ? `Among the ones that are ${st === 'problems' ? 'having problems' : st}.` : null,
    ['Clear search', () => setQuery('')]);
  const said = { on: [`No ${kind} are on`, 'Everything is switched off.'], off: [`No ${kind} are off`, 'Everything installed is switched on.'],
    problems: ['No problems', 'Every server that is on connects, or is waiting to be checked.'] }[st];
  return empty(st === 'problems' ? ICONS.check : ICONS.info, said[0], said[1], ['Show all', () => setFilter({ state: 'any' })]);
}

/** Installed items under small headings by where they come from; problems first in each. */
function groups(rows) {
  const by = new Map();
  for (const it of rows) {
    const g = groupOf(it.scope);
    if (!by.has(g)) by.set(g, []);
    by.get(g).push(it);
  }
  const out = [];
  for (const [g, label, title] of GROUPS) {
    const list = by.get(g);
    if (!list) continue;
    list.sort((a, b) => (problem(b) - problem(a)) || (g === 'wireface' ? 0 : a.name.localeCompare(b.name)));
    out.push(h('section', { class: 'tl-group', 'aria-label': label },
      h('div', { class: 'tl-gh', title }, h('span', {}, label), h('span', { class: 'tl-n' }, String(list.length))),
      h('div', { class: 'tl-rows' }, list.map(rowEl))));
  }
  return out;
}

const rowId = key => `tl-row-${key.replace(/[^\w-]/g, '_')}`;

function shortStatus(it) {
  if (sub === 'skills') {
    if (!it.enabled) return ['off', 'Off'];
    if (it.state === 'name-only') return ['muted', 'Name only'];
    if (it.state === 'user-invocable-only') return ['muted', 'Only when asked'];
    return null;
  }
  if (it.enabled && it.missing) return ['failed', 'Can\'t start'];
  const st = it.status || 'unknown';
  let text = STATUS[st] || st;
  if (st === 'connected' && it.tools != null) text += ` · ${it.tools} tool${it.tools === 1 ? '' : 's'}`;
  return [st, text];
}

/** One line under the name: what it is (the face's own say; a server otherwise shows what it runs). */
function oneLine(it) {
  if (it.description) return it.description;
  if (sub === 'mcp') return tidy(it.target);
  return tidy(it.path);
}

function rowEl(it) {
  const key = it.key, open = opened.has(key), id = rowId(key), mcp = sub === 'mcp';
  const st = shortStatus(it), bad = problem(it);
  const switchKey = `${mcp ? 's' : 'k'}:${key}`;
  let control;
  if (busy.has(switchKey)) control = h('span', { class: 'spin tl-busy', role: 'img', 'aria-label': 'Changing' });
  else if (locked(it)) {
    control = h('button', { class: 'iconbtn tl-lockbtn', title: it.why || 'Can\'t be switched here', 'aria-label': `Why ${it.name} can't be switched here`,
      onclick: () => { opened.add(key); renderBody(); } }, icon(ICON.lock));
  } else {
    control = toggle(it.enabled, on => (mcp ? setServer(it, on) : setSkill(it, on)), `${mcp ? 'MCP server' : 'Skill'} ${it.name}`);
    if (it.setting) control.title = it.why;
  }
  const line = oneLine(it);
  return h('div', { class: `tl-r ${it.enabled ? '' : 'off'} ${bad ? 'problem' : ''} ${open ? 'open' : ''}`, id },
    h('div', { class: 'tl-rtop' },
      h('button', { class: 'tl-rhead', 'aria-expanded': String(open), 'aria-controls': `${id}-d`, dataset: { k: `tl-open:${key}` },
        onclick: () => { if (opened.has(key)) opened.delete(key); else opened.add(key); renderBody(); } },
        h('span', { class: 'tl-chev' }, icon(ICON.chev)),
        h('span', { class: 'tl-rmain' },
          h('span', { class: 'tl-rline' }, h('span', { class: 'tl-rname' }, hl(it.name)),
            h('span', { class: `tl-badge b-${groupOf(it.scope).replace(/\W/g, '')}` }, BADGE[it.scope] || it.scope)),
          h('span', { class: 'tl-rsub' },
            st ? h('span', { class: `tl-st st-${st[0]}` }, h('span', { class: 'tl-dot' }), st[1]) : null,
            line ? h('span', { class: `tl-desc ${!it.description && mcp ? 'mono' : ''}` }, hl(line)) : null))),
      h('div', { class: 'tl-ctl' }, control)),
    open ? details(it, `${id}-d`) : null);
}

function detailRow(label, ...value) {
  return [h('dt', {}, label), h('dd', {}, ...value)];
}

function details(it, id) {
  const mcp = sub === 'mcp';
  const tools = it.tool_names || [];
  const err = it.missing || (mcp && it.status !== 'connected' ? it.detail : '');
  const runs = mcp && it.target ? [it.transport && it.transport !== 'unknown' ? `${it.transport}  ` : '', it.target].join('') : '';
  return h('dl', { class: 'tl-details', id },
    err ? detailRow(it.status === 'failed' || it.missing ? 'Problem' : 'Status', h('span', { class: problem(it) ? 'tl-err' : '' }, hl(err))) : null,
    !mcp && it.description ? detailRow('About', hl(it.description)) : null,
    runs ? detailRow(it.transport === 'stdio' ? 'Runs' : 'Address', h('code', {}, hl(tidy(runs))),
      h('button', { class: 'iconbtn tl-copy', title: 'Copy', 'aria-label': 'Copy', onclick: () => copy(it.target, 'Command') }, icon(ICONS.copy))) : null,
    it.env?.length ? detailRow('Given', h('span', { class: 'tl-chips' }, it.env.map(e => h('code', { class: 'tl-chip' }, hl(e)))),
      h('small', {}, ' (environment variables and headers; their values are never shown)')) : null,
    mcp && (tools.length || it.tools) ? detailRow(`Tools${it.tools ? ` (${it.tools})` : ''}`, tools.length
      ? h('span', { class: 'tl-chips' }, tools.slice(0, 60).map(t => h('code', { class: 'tl-chip' }, hl(t))), tools.length > 60 ? h('small', {}, ` and ${tools.length - 60} more`) : null)
      : h('span', { class: 'tl-faint' }, 'Their names show while the agent runs.')) : null,
    !mcp && it.path ? detailRow('Folder', h('code', { title: it.path }, hl(tidy(it.path))),
      h('button', { class: 'iconbtn tl-copy', title: 'Copy the path', 'aria-label': 'Copy the path', onclick: () => copy(it.path, 'Path') }, icon(ICONS.copy))) : null,
    mcp && it.source && it.scope !== 'wireface' ? detailRow('Defined in', h('code', { title: it.source }, hl(tidy(it.source)))) : null,
    it.writes ? detailRow('Switch', h('span', {}, 'sets ', h('code', {}, it.how), ' in ', h('code', { title: it.writes }, tidy(it.writes)))) : null,
    locked(it) || it.why ? detailRow(locked(it) ? 'Locked' : 'Note', h('span', { class: 'tl-faint' }, it.why || 'Can\'t be switched from here.')) : null);
}

// --- recommended ----------------------------------------------------------------------------------------------------------
function willDo(r) {
  const p = r.plan || {}, agent = view.harness.label;
  if (r.builtin) return [h('span', {}, 'Built into Wireface: nothing to install.')];
  if (p.by === 'wireface' && r.kind === 'skill') return [h('span', {}, 'Wireface copies the skill folder to'), h('code', { title: p.dest }, tidy(p.dest))];
  if (r.kind === 'skill') {                                  // fetched from its source: steps, not one command
    return [h('span', {}, p.by === 'you' ? 'You do this (Codex\'s sandbox can\'t):' : `${agent} is asked to (it asks before each step):`),
      h('span', { class: 'tl-steps' }, hl(tidy(p.command)))];
  }
  const lead = { agent: view.harness.type === 'opencode' ? `${agent} adds this to its config (it asks you first):` : `${agent} runs this (it asks you first):`,
    wireface: 'Wireface runs:', you: 'You do this (the agent\'s sandbox can\'t):' }[p.by] || '';
  return [h('span', {}, lead), h('span', { class: 'tl-cmd' }, h('code', { title: p.command }, hl(p.command || '')),
    h('button', { class: 'iconbtn tl-copy', title: 'Copy', 'aria-label': 'Copy the command', onclick: () => copy(p.command, 'Command') }, icon(ICONS.copy)))];
}

function card(r) {
  const key = `i:${r.id}`;
  let action;
  if (r.installed) {
    action = h('span', { class: 'pill ok tl-installed' }, icon(ICONS.check), r.builtin ? 'Built in' : 'Installed');
  } else if (busy.has(key)) {
    action = h('span', { class: 'pill' }, h('span', { class: 'spin' }), 'Installing');
  } else if (r.installing) {
    action = h('button', { class: 'pill tl-asked', title: 'The agent is installing it: follow it in Activity', onclick: () => openTab('activity#summary') },
      h('span', { class: 'spin' }), 'Asked the agent');
  } else if (r.plan?.by === 'you') {
    action = btn('Copy steps', () => copy(r.plan.command, 'Steps'), { kind: 'sm', ico: ICONS.copy });
  } else {
    action = btn('Install', () => install(r), { kind: 'primary sm', title: r.plan?.command });
  }
  const missing = (r.needs || []).filter(n => n.ok === false);
  return h('article', { class: `tl-card ${r.installed ? 'done' : ''}`, 'aria-label': r.title },
    h('div', { class: 'tl-chead' },
      h('span', { class: 'tl-ckind' }, icon(r.kind === 'mcp' ? ICON.plug : ICON.spark)),
      h('div', { class: 'tl-ctitle' }, h('strong', {}, hl(r.title)), h('span', { class: 'tl-meta' }, [r.by, r.licence].filter(Boolean).join(' · '))),
      action),
    h('p', { class: 'tl-cdesc' }, hl(r.description), r.why ? h('span', { class: 'tl-cwhy' }, ' ', hl(r.why)) : null),
    r.needs?.length ? h('ul', { class: 'tl-needs', 'aria-label': 'Needs' },
      h('li', { class: 'tl-needs-label' }, 'Needs'),
      r.needs.map(n => h('li', { class: n.ok === false ? 'miss' : n.ok ? 'ok' : '', title: n.ok === false ? 'Not found on this PC' : n.ok ? 'Found on this PC' : '' },
        icon(n.ok === false ? ICONS.alert : n.ok ? ICONS.check : ICONS.info), hl(n.label),
        n.ok === false && n.url ? external(n.url, 'Get it') : null)))
      : h('div', { class: 'tl-needs none' }, icon(ICONS.check), r.kind === 'mcp' ? 'Needs nothing else' : 'Nothing to set up'),
    missing.length && !r.installed ? h('div', { class: 'tl-warn' }, icon(ICONS.alert),
      h('span', {}, missing.map(n => n.hint || `${n.label} isn't on this PC yet: it won't start without it.`).join(' '))) : null,
    r.auth ? h('div', { class: 'tl-auth' }, icon(ICONS.key), r.auth) : null,
    !r.installed ? h('div', { class: 'tl-will' }, ...willDo(r)) : null,
    h('div', { class: 'tl-cfoot' },
      r.homepage ? external(r.homepage, 'About') : h('span'),
      r.installed && r.installed_key ? h('button', { class: 'linkbtn tl-showit', onclick: () => showIt(r.installed_key) }, 'Show it', icon(ICON.arrow)) : null));
}
