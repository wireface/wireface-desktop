// The Voice and Agent tabs: this face's own config (its profile.json). Most controls save as they change; the agent
// settings that need a new agent process (which agent, model, folder, skipping permissions, its tools) wait until
// "Apply and restart" sends them together, and the Agent tab is locked until the agent is back with them.
// The server validates, and a refusal puts the control back as it was.
import { $, h, send, SHELL } from './common.js';
import { group, row, toggle, select, text, seg, btn, note, icon, ICONS, keepFocus, noWheel } from './ui.js';
import { VoiceMeter, FATES, STRONG_DB, SOME_DB } from '../face/meter.js';

let view = null;                 // GET /api/config
let live = null;                 // the latest status message
let toast = () => {};
let progress = null;             // speech model download: { key, done, total, phase, error }
let confirming = false;          // the "skip permissions" warning is showing
let replacingKey = false;        // the ElevenLabs key box is open although a key exists
let voices = null;               // the account's ElevenLabs voices (loaded when needed) or an Error
let lastSig = '';
let draft = {};                  // backend settings changed but not applied yet: key -> value
let applying = null;             // { sent, timer, until } while the agent restarts with them

export const setToast = fn => { toast = fn; };

const roots = () => [$('tab-voice'), $('tab-agent')];
const nest = (path, value) => path.reduceRight((acc, key) => ({ [key]: acc }), value);

async function save(path, value) {
  try {
    view = await send('/api/config', 'PUT', nest(path, value));
  } catch (e) {
    toast(e.message);
  }
  render();
}

export async function init() {
  view = await send('/api/config');
  render();
}

export function status(s) {
  const beaconWas = live?.beacon?.state;
  live = s;
  if (detector) detector.meter.idle = s.room ? 'In a room, the room listens' : !s.mode?.listening ? 'Listening is off' : '';
  if (!view) return;
  if (applying && s.beacon?.state !== beaconWas && !typing()) render();       // "restarting" / "waits for the reply"
  // the choices on offer (voices, speech models) can change underneath: refresh the view when they do
  const sig = JSON.stringify([s.voices, s.speech, s.backend, s.cwd, s.skip_permissions, s.voice_engine, s.voice_mode,
    s.realtime && [s.realtime.state, s.realtime.error], s.room && [s.room.connected, s.room.members], s.phone]);
  if (sig !== lastSig) {
    lastSig = sig;
    send('/api/config').then(v => { view = v; settled(); if (!typing()) render(); }).catch(() => {});
  }
}

// --- agent settings that wait for "Apply and restart" ------------------------------------------------------------------
const APPLY_TIMEOUT_MS = 120000;  // then the tab unlocks anyway (the restart still happens when the agent can)

/** A backend setting as it will be: the change waiting to be applied, else the saved one. */
const pick = key => (key in draft ? draft[key] : view.config.backend[key]);
const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
const pending = () => Object.keys(draft).filter(k => !same(draft[k], view.config.backend[k]));
const agentLabel = type => view.backends.find(x => x.type === type)?.label || type;

function stage(key, value) {
  draft[key] = value;
  if (key === 'type') {
    // a model belongs to its agent: another agent starts on its own default unless one is picked
    if (same(value, view.config.backend.type)) delete draft.model;
    else draft.model = null;
  }
  for (const k of Object.keys(draft)) if (same(draft[k], view.config.backend[k])) delete draft[k];
  render();
}

function discard() {
  draft = {};
  confirming = false;
  render();
}

async function apply() {
  const keys = pending();
  if (applying || !keys.length) return;
  const patch = Object.fromEntries(keys.map(k => [k, draft[k]]));
  applying = { sent: false, until: Date.now() + APPLY_TIMEOUT_MS };
  render();
  try {
    view = await send('/api/config', 'PUT', { backend: patch });
  } catch (e) {
    applying = null;
    toast(e.message);
    render();
    return;
  }
  draft = {};
  applying.sent = true;
  // the status messages say when it is back; asking as well covers a restart that was over before they arrived
  applying.timer = setInterval(() => send('/api/config').then(v => {
    const was = applying;
    view = v;
    settled();
    if (was !== applying && !typing()) render();
  }).catch(() => {}), 1000);
  settled();
  render();
}

/** After a refresh of the view: is the agent up with the applied settings (or has it given up for now)? */
function settled() {
  if (!applying?.sent) return;
  const timedOut = Date.now() > applying.until;
  if (view.backend.applying && !timedOut) return;
  clearInterval(applying.timer);
  applying = null;
  const b = live?.beacon, label = view.backend.label;
  if (timedOut) toast(`${label} hasn't restarted yet: it will once it can. The settings are saved.`);
  else if (b?.state === 'error') toast(b.label);
  else if (!view.config.backend.auto_start && b?.state === 'offline') toast(`Saved. ${label} starts with them on your next message.`);
  else toast(`${label} restarted with the new settings.`);
}

/** The bar under the Agent tab while changes wait (or are being applied). */
function applyBar() {
  const keys = pending();
  if (!applying && !keys.length) return null;
  if (applying) {
    const label = agentLabel(pick('type'));
    const waiting = live?.beacon?.state === 'working';                 // a restart waits for the reply in progress
    return h('div', { class: 'applybar busy', role: 'status' }, h('span', { class: 'spin' }),
      h('div', { class: 'applytext' }, waiting ? `${label} restarts once it finishes what it's doing...` : `Restarting ${label}...`));
  }
  const folder = keys.includes('cwd');
  const n = keys.filter(k => !(k === 'model' && keys.includes('type') && draft.model === null)).length;   // not the model reset
  return h('div', { class: 'applybar', role: 'region', 'aria-label': 'Changes to apply' }, icon(ICONS.info),
    h('div', { class: 'applytext' }, `${n === 1 ? 'A change' : `${n} changes`} waiting. ` +
      (folder ? 'The new folder starts a new conversation.' : 'The agent restarts; the conversation continues.')),
    btn('Discard', discard, { kind: 'ghost sm' }),
    btn('Apply and restart', apply, { kind: 'primary sm' }));
}

/** The tab came into view; `anchor` scrolls to (and for "skip", opens) one setting. */
export function shown(anchor) {
  if (anchor === 'skip' && view && !view.config.backend.skip_permissions) confirming = true;
  for (const kind of Object.keys(modelLists)) if (!modelLists[kind].loading) delete modelLists[kind];   // pick up a refresh
  render();
  const target = { skip: 'skip-row', folder: 'folder-row', room: 'room-group', speech: 'speech-row', phone: 'phone-group' }[anchor];
  if (target) {
    const el = $(target);
    el?.scrollIntoView({ block: 'center' });
    el?.classList.add('flash');
    setTimeout(() => el?.classList.remove('flash'), 1400);
  }
}

export function models(msg) {
  progress = msg.phase === 'done' ? null : msg;
  if (msg.phase === 'error') toast(msg.error || 'The download failed');
  if (msg.phase === 'done') { toast('Speech models installed.'); send('/api/config').then(v => { view = v; render(); }); }
  if (!typing()) render();
}

const typing = () => roots().some(r => r.contains(document.activeElement)) && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);

// --- models: a list for each agent and realtime service (GET /api/models, see catalog.py), or "Other..." to type one --
const modelLists = {};           // kind ("claude", "realtime:gemini", ...) -> { groups, error, loading }
const modelOther = new Set();    // kinds whose "Other..." box is open
const OTHER = '__other__';

function loadModels(kind, refresh = false) {
  if (modelLists[kind] && !refresh) return modelLists[kind];
  const entry = modelLists[kind] = { loading: true, groups: modelLists[kind]?.groups || [] };
  send(`/api/models?backend=${encodeURIComponent(kind)}${refresh ? '&refresh=1' : ''}`)
    .then(d => { modelLists[kind] = { groups: d.groups || [], error: d.error || null }; })
    .catch(e => { modelLists[kind] = { groups: entry.groups, error: e.message }; })
    .finally(() => { if (!typing()) render(); });
  return entry;
}

/** A model, picked from the list or typed in under "Other...". defaultLabel: an empty choice (the agent decides). */
function modelPicker(kind, current, onSave, { defaultLabel = null, placeholder = 'Model name' } = {}) {
  const list = loadModels(kind);
  const known = new Set(list.groups.flatMap(g => g.models.map(m => m.id)));
  if (defaultLabel !== null) known.add('');
  const typed = !!current && !known.has(current);
  const other = modelOther.has(kind) || (typed && !list.loading);
  const sel = noWheel(h('select', { 'aria-label': 'Model', dataset: { k: `model:${kind}` }, onchange: () => {
    if (sel.value === OTHER) {
      modelOther.add(kind);
      render();
      document.querySelector(`[data-k="model-other:${CSS.escape(kind)}"]`)?.focus();
      return;
    }
    modelOther.delete(kind);
    onSave(sel.value);
  } },
    defaultLabel !== null ? h('option', { value: '', selected: !other && !current }, defaultLabel) : null,
    typed && list.loading ? h('option', { value: current, selected: true }, current) : null,
    list.loading && !list.groups.length ? h('option', { disabled: true }, 'Loading the list...') : null,
    list.groups.map(g => h('optgroup', { label: g.label },
      g.models.map(m => h('option', { value: m.id, title: m.id, selected: !other && m.id === current }, m.name)))),
    h('option', { value: OTHER, selected: other }, 'Other...')));
  const box = other ? text(typed ? current : '', val => {
    if (!val) return;
    if (known.has(val)) modelOther.delete(kind);
    onSave(val);
  }, { placeholder }) : null;
  if (box) box.dataset.k = `model-other:${kind}`;
  return h('div', { class: 'modelpick' }, sel, box);
}

/** The row's help, with a link that downloads the list again (RefreshModels.cmd does the same from outside). */
function modelHelp(kind, ok) {
  const list = modelLists[kind];
  const again = h('a', { href: '#', onclick: e => { e.preventDefault(); loadModels(kind, true); render(); } },
    list?.loading ? 'Downloading...' : 'Refresh the list');
  if (list?.error && !list.groups.length) return h('span', {}, `Couldn't get the list of models (${list.error}). Choose Other to type a name. `, again);
  return h('span', {}, ok ? `${ok} ` : '', again);
}

// --- API keys (ElevenLabs, the realtime services): saved here, never shown again -----------------------------------
function keyRow({ label, available, source, replacing, setReplacing, onSave, onRemove, envName, title = 'API key' }) {
  if (available && !replacing) {
    const where = source === 'saved' ? 'Saved for this face' : `From the ${source.slice(4)} environment variable`;
    return row(title, h('div', { class: 'keyline' },
      h('span', { class: 'keystate' }, icon(ICONS.key), h('span', {}, where)),
      btn(source === 'saved' ? 'Replace' : 'Use another', () => setReplacing(true), { kind: 'sm' }),
      source === 'saved' ? btn('Remove', onRemove, { kind: 'ghost sm' }) : null), null, { class: 'stack' });
  }
  const input = h('input', { type: 'password', placeholder: `Paste your ${label} ${title}`, autocomplete: 'off', spellcheck: 'false' });
  const go = () => { if (input.value.trim()) onSave(input.value.trim()); };
  input.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
  return row(title, h('div', { class: 'keyline' }, input, btn('Save', go, { kind: 'primary sm' }),
    replacing ? btn('Cancel', () => setReplacing(false), { kind: 'ghost sm' }) : null),
  `Or set the ${envName} environment variable; a new Windows variable is picked up within seconds.`, { class: 'stack' });
}

// --- ElevenLabs ----------------------------------------------------------------------------------------------------
async function setKey(key) {
  try {
    view = await send('/api/voice/elevenlabs/key', 'PUT', { key });
    replacingKey = false;
    voices = null;
    toast(key ? 'ElevenLabs key saved.' : 'Saved key removed.');
  } catch (e) {
    toast(e.message);
  }
  render();
}

async function loadVoices() {
  voices = 'loading';
  render();
  try {
    voices = (await send('/api/voice/elevenlabs/voices')).voices;
  } catch (e) {
    voices = new Error(e.message);
  }
  render();
}

function elevenlabsRows(v) {
  const el = view.elevenlabs;
  if (el.available && voices === null) loadVoices();
  const list = Array.isArray(voices) ? voices : [];
  const chosen = v.elevenlabs.voice_id;
  const options = [['', voices === 'loading' && !list.length ? 'Loading voices...' : 'First voice on the account'],
    ...list.map(x => [x.id, x.category && x.category !== 'premade' ? `${x.name} (${x.category})` : x.name])];
  if (chosen && !list.some(x => x.id === chosen)) options.push([chosen, chosen]);
  const models = Object.entries(el.models);
  if (!models.some(([id]) => id === v.elevenlabs.model_id)) models.push([v.elevenlabs.model_id, v.elevenlabs.model_id]);
  const test = () => send('/api/voice/test', 'POST')
    .then(r => toast(`Speaking with ${r.engine === 'elevenlabs' ? 'ElevenLabs' : 'the voice on this PC'}.`)).catch(e => toast(e.message));

  return [
    h('div', { class: 'subhead' }, 'ElevenLabs'),
    keyRow({ label: 'ElevenLabs', available: el.available, source: el.source, replacing: replacingKey,
      setReplacing: on => { replacingKey = on; render(); }, onSave: setKey, onRemove: () => setKey(null), envName: v.elevenlabs.api_key_env }),
    el.available ? row('Voice', h('div', { class: 'keyline' },
      select(options, chosen || '', val => save(['voice', 'elevenlabs', 'voice_id'], val || null)),
      h('button', { class: 'iconbtn', title: 'Reload the voices', 'aria-label': 'Reload the voices', onclick: loadVoices }, icon(ICONS.refresh))),
    voices instanceof Error ? `Could not load the voices: ${voices.message}` : null, { class: 'wide' }) : null,
    el.available ? row('Quality', select(models, v.elevenlabs.model_id, val => save(['voice', 'elevenlabs', 'model_id'], val)),
      'Faster models start speaking sooner.') : null,
    el.available ? row('Try it', btn('Test voice', test, { ico: ICONS.speaker, kind: 'sm' })) : null,
    h('p', { class: 'privacy' }, 'While ElevenLabs is used, the text of replies is sent to ElevenLabs to be turned into speech. The microphone and speech recognition stay on this PC.'),
  ];
}

// --- the realtime voice ----------------------------------------------------------------------------------------------
let replacingRtKey = false;      // the realtime key box is open although a key exists

async function setRealtimeKey(provider, key) {
  try {
    view = await send('/api/voice/realtime/key', 'PUT', { provider, key });
    replacingRtKey = false;
    toast(key ? 'Key saved.' : 'Saved key removed.');
  } catch (e) {
    toast(e.message);
  }
  render();
}

function realtimeState(rt, fallback) {
  if (!rt) return note('', 'Not running (the voice is off, or this face is in a room).');
  const problem = rt.problem || `${rt.label} can't connect right now.`;
  const then = fallback ? 'The voice on this PC reads the replies meanwhile.' : 'Replies show as text meanwhile.';
  const text = { connected: `Connected to ${rt.label}.`, connecting: `Connecting to ${rt.label}...`,
    retrying: `${problem} It keeps trying to reconnect. ${then}`,
    failed: `${problem} ${then}`, off: 'Off.' }[rt.state] || rt.state;
  return note(rt.state === 'connected' ? 'ok' : rt.state === 'failed' || rt.state === 'retrying' ? 'bad' : '', text);
}

function realtimeGroup(c) {
  const r = c.voice.realtime, info = view.realtime, pid = r.provider, p = info.providers[pid], mine = r[pid];
  const voiceList = p.voices.map(v => [v, v]);
  if (!p.voices.includes(mine.voice)) voiceList.push([mine.voice, mine.voice]);
  return group({ title: 'Realtime voice', id: 'realtime-group' },
    realtimeState(view.realtime.live, r.fallback),
    row('Service', select(Object.entries(info.providers).map(([id, x]) => [id, x.label + (x.available ? '' : ' (no key)')]), pid,
      val => save(['voice', 'realtime', 'provider'], val)), 'The speech-to-speech model you talk with.'),
    keyRow({ label: p.label, available: p.available, source: p.source, replacing: replacingRtKey,
      setReplacing: on => { replacingRtKey = on; render(); }, onSave: key => setRealtimeKey(pid, key),
      onRemove: () => setRealtimeKey(pid, null), envName: mine.api_key_env }),
    row('Model', modelPicker(`realtime:${pid}`, mine.model, val => save(['voice', 'realtime', pid, 'model'], val || p.default_model),
      { placeholder: p.default_model }), modelHelp(`realtime:${pid}`, p.available ? `The models your ${p.label} key can use.` : null)),
    row('Voice', select(voiceList, mine.voice, val => save(['voice', 'realtime', pid, 'voice'], val))),
    row('Updates', select([['quiet', 'Quiet: replies, questions and problems'], ['normal', 'Normal: also progress every 25 s or so'],
      ['chatty', 'Chatty: progress every 10 s or so']], r.narration, val => save(['voice', 'realtime', 'narration'], val)),
    'How often it tells you what the agent is doing while it works. You can always ask.', { class: 'wide' }),
    row('Speaks as', select([['self', 'The agent: "I\'m running the tests"'], ['relay', `Its voice: "${view.backend.label} is running the tests"`]],
      r.persona, val => save(['voice', 'realtime', 'persona'], val)), null, { class: 'wide' }),
    row('Fall back to classic voice', toggle(r.fallback, on => save(['voice', 'realtime', 'fallback'], on), 'Fall back to classic voice'),
      r.fallback ? 'If the realtime voice can\'t connect, the voice on this PC reads the replies until it\'s back.'
        : 'Off: if the realtime voice can\'t connect you get a message saying why, and replies show as text.'),
    row('Interrupt by talking', toggle(r.barge_in, on => save(['voice', 'realtime', 'barge_in'], on), 'Interrupt by talking'),
      'Talk over it to cut it off. Use headphones: through speakers it would hear itself. When off, the microphone rests while it speaks (Esc stops it).'),
    h('p', { class: 'privacy' }, `${p.label} never hears you: speech recognition runs on this PC and your words go to ` +
      `${view.backend.label} exactly as recognised. ${p.label} gets the text of what you said, short notes about the agent's ` +
      'work and its replies, and speaks only when the face asks it to. Saying just "stop" stops both.'));
}

// --- advanced: filters for noisy rooms -------------------------------------------------------------------------------------
let replacingJudgeKey = false;   // the OpenRouter key box is open although a key exists

async function setJudgeKey(key) {
  try {
    view = await send('/api/voice/judge/key', 'PUT', { key });
    replacingJudgeKey = false;
    toast(key ? 'OpenRouter key saved.' : 'Saved key removed.');
  } catch (e) {
    toast(e.message);
  }
  render();
}

function judgeState(j) {
  if (!j.available) return note('', 'Needs an OpenRouter API key. Until there is one, everything goes to the agent as before.');
  if (j.error) return note('bad', `Last try: ${j.error}. Meanwhile what it hears goes to the agent as before.`);
  if (!j.calls) return note('', 'Ready: it judges the next thing you say.');
  return note('ok', `Judged ${j.calls} thing${j.calls === 1 ? '' : 's'} this session, for $${j.spent_usd.toFixed(4)}.`);
}

// --- the voice detector: made once, so a redraw of the tab moves it rather than starting it again --------------------------
let detector = null;             // { box, meter, list }

export function meter(msg) {
  detector?.meter.push(msg);
}

const signedDb = db => `${db > 0 ? '+' : ''}${Math.round(db)} dB`;

function heardRow(m) {
  const zone = m.lift == null ? 'unknown' : m.lift >= STRONG_DB ? 'strong' : m.lift >= SOME_DB ? 'some' : 'low';
  const words = m.text ? `“${m.text}”` : m.fate === 'empty' ? 'no words recognised' : 'not transcribed';
  const more = [m.why ? `kept: ${m.why}` : null, m.vs_user != null ? `${signedDb(m.vs_user)} against your voice` : null,
    `${m.dur.toFixed(1)} s`].filter(Boolean).join(' · ');
  return h('li', { class: `vheard ${m.fate}` },
    h('span', { class: `vlift ${zone}` }, m.lift == null ? 'learning' : signedDb(m.lift)),
    h('span', { class: 'vfate' }, FATES[m.fate] || m.fate),
    h('span', { class: 'vwords' }, words, h('small', {}, more)));
}

function detectorBox() {
  if (detector) return detector.box;
  const canvas = h('canvas', { 'aria-label': 'Voice detector: how far what the microphone hears stands out from the room' });
  const list = h('ol', { class: 'vlist' }, h('li', { class: 'vnone' }, 'What it hears shows up here.'));
  const box = h('div', { class: 'vmeter' }, h('div', { class: 'vcanvas' }, canvas), list,
    h('p', { class: 'vkey' }, 'Bars: each 50 ms of sound, bright while it hears speech; green stands out strongly (+12 dB), ' +
      'amber somewhat (+6 dB). Lines: each thing said, at how far it stood out. Shaded: the face was talking (it doesn\'t listen then).'));
  const m = new VoiceMeter(canvas, { seconds: 12 });
  m.onUtterance = u => {
    list.querySelector('.vnone')?.remove();
    list.prepend(heardRow(u));
    while (list.children.length > 8) list.lastChild.remove();
  };
  detector = { box, meter: m, list };
  return box;
}

function advancedGroup(c) {
  const hr = c.voice.hearing, j = hr.judge, info = view.judge;
  if (hr.show_detector) {
    detectorBox();
    detector.meter.idle = live?.room ? 'In a room, the room listens' : !c.voice.listening ? 'Listening is off' : '';
    requestAnimationFrame(() => detector.meter.start());
  }
  return group({ title: 'Advanced', note: 'Extra filters for noisy rooms. With both off, everything said nearby goes to the agent.' },
    row('Ignore constant background sound', toggle(hr.surprise, on => save(['voice', 'hearing', 'surprise'], on), 'Ignore constant background sound'),
      'Learns what the room usually sounds like (a TV, radio, music, people chatting) and only listens to speech that stands out ' +
      'from it, or is said at your level. Runs on this PC.'),
    row('Show voice detector', toggle(hr.show_detector, on => save(['voice', 'hearing', 'show_detector'], on), 'Show voice detector'),
      'Shows live, on the face and here, how far what the microphone hears stands out from what the room usually sounds like, ' +
      'and what became of each thing said. ' + (hr.surprise ? 'The filter above is on: lines marked "ignored" were dropped.'
        : 'The filter above is off: lines marked "would ignore" still went to the agent.')),
    hr.show_detector ? detector.box : null,
    row('Judge what it hears', toggle(j.on, on => save(['voice', 'hearing', 'judge', 'on'], on), 'Judge what it hears'),
      'Before the agent gets anything, a fast classifier decides whether it was said to the agent at all: talk to someone else, ' +
      'the TV, "yeah", "mm" and noise are dropped, a sentence cut off in the middle waits for the rest, and "stop", yes/no and ' +
      '"go away" work in your own words.'),
    j.on ? judgeState(info) : null,
    j.on ? keyRow({ label: 'OpenRouter', available: info.available, source: info.source, replacing: replacingJudgeKey,
      setReplacing: on => { replacingJudgeKey = on; render(); }, onSave: setJudgeKey, onRemove: () => setJudgeKey(null),
      envName: j.api_key_env }) : null,
    j.on ? row('Model', text(j.model, val => save(['voice', 'hearing', 'judge', 'model'], val || info.default_model),
      { placeholder: info.default_model }), 'A decision model on OpenRouter (Jev by TypeSafe: about half a second, a fraction of a cent each).') : null,
    j.on ? row("When it isn't sure", select([['hold', 'Hold it back: the activity panel has a Send button'], ['send', 'Send it to the agent anyway']],
      j.unsure, val => save(['voice', 'hearing', 'judge', 'unsure'], val)), null, { class: 'wide' }) : null,
    h('p', { class: 'privacy' }, 'With "Judge what it hears" on, the words you say (never the sound) and the last few lines of the ' +
      'conversation go to OpenRouter, which passes them to TypeSafe in the US to be judged.'));
}

// --- speech models ------------------------------------------------------------------------------------------------
function speechControl(speech) {
  const missing = speech.missing || [];
  if (progress && progress.phase !== 'error') {
    return h('div', { class: 'installing' },
      h('div', { class: 'istate' }, progress.phase === 'unpack' ? 'Unpacking...' : `Downloading ${progress.key}...`,
        h('span', {}, progress.total ? `${(progress.done / 1e6).toFixed(0)} of ${(progress.total / 1e6).toFixed(0)} MB` : '')),
      h('div', { class: `bar ${progress.total ? '' : 'indeterminate'}` },
        h('span', { style: progress.total ? `width:${(100 * progress.done / progress.total).toFixed(1)}%` : '' })));
  }
  if (speech.installing) return h('div', { class: 'installing' }, h('div', { class: 'istate' }, 'Installing...'), h('div', { class: 'bar indeterminate' }, h('span')));
  if (missing.length) {
    return h('div', { class: 'missing' },
      h('div', {}, `Missing: ${missing.join(', ')}. About ${speech.install_mb} MB to download.`),
      btn('Install speech models', () => send('/api/speech/install', 'POST').catch(e => toast(e.message)), { kind: 'primary sm' }));
  }
  return h('span', { class: 'pill ok' }, icon(ICONS.check), 'All installed');
}

// --- the Voice tab --------------------------------------------------------------------------------------------------
function voiceTab() {
  const { config: c, speech } = view;
  const v = c.voice;
  const realtime = v.mode === 'realtime';
  const engineHelp = live?.voice_engine ? (live.voice_engine === 'realtime' ? 'Automatic uses ElevenLabs whenever a key is available.'
    : `Speaking with ${live.voice_engine === 'elevenlabs' ? 'ElevenLabs' : 'the voice on this PC'}. Automatic uses ElevenLabs whenever a key is available.`) : null;
  return [
    group({ title: 'Talking' },
      row('Spoken replies', toggle(v.on, on => save(['voice', 'on'], on), 'Spoken replies'), v.on ? 'Replies are spoken aloud.' : 'Off: replies are shown as captions.'),
      row('Listening', toggle(v.listening, on => save(['voice', 'listening'], on), 'Listening'), v.hearing.judge.on || v.hearing.surprise
        ? 'Use the microphone. What is said nearby goes to the agent, through the filters under Advanced.'
        : 'Use the microphone. While on, anything said nearby goes to the agent.'),
      realtime ? null : row('Voice', btn('Use the realtime voice', () => save(['voice', 'mode'], 'realtime'), { kind: 'sm' }),
        'This face uses the classic voice (speech on this PC or ElevenLabs reading the replies).')),
    realtime && v.on ? realtimeGroup(c) : null,
    // the classic voice's own settings only matter while it is the voice; as a fallback it is just a switch
    realtime ? null : group({ title: 'Classic voice' },
      row('Voice engine', seg([['auto', 'Automatic'], ['local', 'This PC'], ['elevenlabs', 'ElevenLabs']], v.engine,
        val => save(['voice', 'engine'], val), { label: 'Voice engine', cls: 'wide' }), engineHelp, { class: 'stack' }),
      view.voices.length ? row(v.engine === 'local' ? 'Voice' : 'Voice on this PC', select(view.voices.map(x => [x, x[0].toUpperCase() + x.slice(1)]), v.local.voice,
        val => save(['voice', 'local', 'voice'], val))) : null,
      ...(v.engine !== 'local' ? elevenlabsRows(v) : [])),
    group({ title: 'Hearing' },
      row('Say its name to interrupt', toggle(v.hearing.name_interrupt, on => save(['voice', 'hearing', 'name_interrupt'], on), 'Say its name to interrupt'),
        `Say "${c.name}" while it talks: it stops within about half a second and listens to what you say next.` +
        (c.room.aliases.length ? ` Also: ${c.room.aliases.join(', ')}.` : ' Other names can be added under Agent > Room.')),
      row('Reduce background noise', toggle(v.hearing.denoise, on => save(['voice', 'hearing', 'denoise'], on), 'Reduce background noise'),
        'Takes out steady noise (fans, hum, hiss, typing) before your words are recognised.'),
      row('Ignore distant voices', toggle(v.hearing.focus, on => save(['voice', 'hearing', 'focus'], on), 'Ignore distant voices'),
        'Drops speech much quieter than yours, like a TV or people across the room. It learns your level as you talk.'),
      row('Speech models', speechControl(speech), 'Speech recognition and the voice run on this PC.', { id: 'speech-row', class: 'wide' })),
    advancedGroup(c),
  ];
}

// --- the phone line (Twilio) ---------------------------------------------------------------------------------------------
let replacingPhoneToken = false;
const DAY_NAMES = [['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'], ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun']];

async function setPhoneToken(key) {
  try {
    view = await send('/api/phone/token', 'PUT', { key });
    replacingPhoneToken = false;
    toast(key ? 'Auth token saved.' : 'Saved token removed.');
  } catch (e) {
    toast(e.message);
  }
  render();
}

/** A box for a few lines of text, saved when it loses focus. */
function area(value, onChange, { placeholder = '', rows = 4 } = {}) {
  const el = h('textarea', { rows, placeholder, spellcheck: 'false', onchange: () => onChange(el.value) });
  el.value = value ?? '';
  return el;
}

const contactsText = c => Object.entries(c).map(([name, num]) => `${name}: ${num}`).join('\n');
const parseContacts = t => Object.fromEntries(t.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
  const i = l.lastIndexOf(':');
  return i > 0 ? [l.slice(0, i).trim(), l.slice(i + 1).trim()] : [l, ''];
}));

function phoneState(p, live) {
  if (!p.enabled) return null;
  const s = live?.state;
  if (s === 'starting') return note('', 'Starting...');
  if (s === 'error' || (s === 'ready' && live.error)) return note('bad', live.error);
  if (s === 'ready') return note('ok', live.public_url ? `Answering texts and calls on ${p.number}.` : `Answering texts on ${p.number}.`);
  return note('', 'Not running.');
}

function hoursEditor(hours) {
  return h('div', { class: 'hours' }, DAY_NAMES.map(([d, label]) => h('label', {}, label,
    text(hours[d] || '', val => save(['phone', 'receptionist', 'hours'], { ...hours, [d]: val || null }), { placeholder: 'closed' }))));
}

function phoneGroup(c) {
  const p = c.phone, r = p.receptionist, live = view.phone;
  const intro = 'A Twilio number of your own: a receptionist answers its calls and texts, and your agent can text and call from it.';
  if (!p.enabled) {
    return group({ title: 'Phone', id: 'phone-group', note: intro },
      row('Phone line', toggle(false, on => save(['phone', 'enabled'], on), 'Phone line'), 'Needs a Twilio account and number.'));
  }
  const test = () => send('/api/phone/test', 'POST').then(x => toast(x.message)).catch(e => toast(e.message));
  const call = p.models.call_provider;
  return group({ title: 'Phone', id: 'phone-group', note: intro },
    phoneState(p, live),
    row('Phone line', toggle(true, on => save(['phone', 'enabled'], on), 'Phone line')),
    row('Account SID', text(p.account_sid, val => save(['phone', 'account_sid'], val || null), { placeholder: 'AC...' }),
      'From the Twilio console.'),
    keyRow({ label: 'Twilio', title: 'Auth token', available: live.token.available, source: live.token.source,
      replacing: replacingPhoneToken, setReplacing: on => { replacingPhoneToken = on; render(); },
      onSave: setPhoneToken, onRemove: () => setPhoneToken(null), envName: p.auth_token_env }),
    row('Number', text(p.number, val => save(['phone', 'number'], val || null), { placeholder: '+44 7700 900123' }),
      'Your Twilio number, with its country code.'),
    row('Check', btn('Check the account', test, { kind: 'sm' }), 'Nothing is sent or called.'),
    row('Your name', text(p.owner, val => save(['phone', 'owner'], val || null), { placeholder: 'Sam Jones' }),
      'Who the phone agent works for: "calling on behalf of ...".'),
    h('div', { class: 'subhead' }, 'Your agent\'s texts and calls'),
    row('Contacts', area(contactsText(p.contacts), val => save(['phone', 'contacts'], parseContacts(val)), { placeholder: 'Priya: +44 7700 900200' }),
      'One per line, name: number. The agent can text and call these by name. Every text and call is put to you first, whoever it\'s to.', { class: 'stack' }),
    row('Countries', text(p.allow_countries.join(', '), val => save(['phone', 'allow_countries'], val.split(',').map(x => x.trim()).filter(Boolean))),
      'Country codes texts and calls may go to. Premium-rate and special-service numbers are always blocked.'),
    row('Texts a day', select([0, 5, 10, 20, 50, 100].map(n => [String(n), String(n)]), String(p.limits.texts_per_day),
      val => save(['phone', 'limits', 'texts_per_day'], Number(val)))),
    row('Calls a day', select([0, 3, 10, 25, 50].map(n => [String(n), String(n)]), String(p.limits.calls_per_day),
      val => save(['phone', 'limits', 'calls_per_day'], Number(val)))),
    h('div', { class: 'subhead' }, 'The receptionist'),
    row('Incoming texts', select([['receptionist', 'The receptionist answers'], ['notify', 'Tell me; don\'t answer'], ['off', 'Ignore']], p.texts,
      val => save(['phone', 'texts'], val)), 'Texts from your contacts, and replies from people you texted, are always passed to you instead.'),
    row('Incoming calls', select([['receptionist', 'The receptionist answers'], ['off', 'Not answered']], p.calls,
      val => save(['phone', 'calls'], val))),
    row('Business name', text(r.business, val => save(['phone', 'receptionist', 'business'], val || null), { placeholder: 'Bloom Hair Studio' })),
    row('What callers may know', area(r.about, val => save(['phone', 'receptionist', 'about'], val), { rows: 5,
      placeholder: 'Services and prices, address, parking, how to pay...' }),
    'The receptionist answers from this alone. It never sees your agent, files, email or computer.', { class: 'stack' }),
    row('Take bookings', toggle(r.bookings, on => save(['phone', 'receptionist', 'bookings'], on), 'Take bookings'),
      'Callers can book, move and cancel their own appointments in Wireface\'s booking list.'),
    r.bookings ? row('Opening hours', hoursEditor(r.hours), 'HH:MM-HH:MM, or empty when closed.', { class: 'stack' }) : null,
    r.bookings ? row('Appointments', select([15, 20, 30, 45, 60, 90, 120].map(n => [String(n), `${n} minutes`]), String(r.slot_minutes),
      val => save(['phone', 'receptionist', 'slot_minutes'], Number(val)))) : null,
    h('div', { class: 'subhead' }, 'Live calls'),
    row('Voice', select([['gemini', 'Gemini Live'], ['openai', 'OpenAI Realtime']], call, val => save(['phone', 'models'],
      val === 'gemini' ? { call_provider: 'gemini', call: 'gemini-3.8-live', call_voice: 'Puck' }
        : { call_provider: 'openai', call: 'gpt-realtime-2.1', call_voice: 'marin' })), 'Uses that service\'s key from Settings > Voice.'),
    row('Public address', text(p.public_url, val => save(['phone', 'public_url'], val || null),
      { placeholder: live.cloudflared ? 'Automatic: a Cloudflare tunnel' : 'https://...' }),
    live.cloudflared ? 'Calls reach a small phone server of its own (never the face itself). Empty: a Cloudflare tunnel is started.'
      : 'Live calls need an https address that reaches this PC: install cloudflared, or enter your own. Texts work without one.'),
    row('Manage the number', toggle(p.manage_number, on => save(['phone', 'manage_number'], on), 'Manage the number'),
      'When it starts, point the number\'s calls here and turn off Twilio\'s demo reply to texts.'),
    h('p', { class: 'privacy' }, 'Safe by design: whoever calls or texts talks to a receptionist that can only take messages, ' +
      'look up and book appointments, and reply to them. Your agent\'s texts and calls are always put to you first, only to the ' +
      'countries above, never to premium-rate numbers. Every conversation is kept as a transcript in this face\'s folder.'));
}

// --- the Agent tab ---------------------------------------------------------------------------------------------------
function roomGroup(c, live) {
  const room = c.room;
  const name = (live && live.name) || room.name;                    // the room it is in (--room can differ from the config)
  const members = live && live.connected ? live.members : [];
  const lead = [['first', 'Whoever answers first'], ...members.map(m => [m, `${m} always leads`])];
  if (room.lead !== 'first' && !lead.some(([v]) => v === room.lead)) lead.push([room.lead, `${room.lead} always leads`]);
  const viaFlag = live && live.name !== room.name;
  return group({ title: 'Room', id: 'room-group', note: 'Faces with the same room name share one microphone and take turns: one speaks at a time, they read each other as text, and you can interrupt by saying a name.' },
    row('Room name', text(name, val => save(['room', 'name'], val || null), { placeholder: 'None: works alone' }),
      viaFlag ? 'This face joined with --room; a name typed here is remembered for next time.' : null),
    name ? row('Also called', text(room.aliases.join(', '), val => save(['room', 'aliases'], val ? val.split(',').map(x => x.trim()).filter(Boolean) : [])),
      'Other ways of saying this agent\'s name (speech recognition can mishear it), separated by commas.') : null,
    name ? row('Who leads', select(lead, room.lead, val => save(['room', 'lead'], val)),
      'The lead answers first and has the last word. The others comment in between.') : null,
    name ? row('In the room', live && live.connected
      ? h('div', { class: 'members' }, members.map(m => h('span', { class: 'pill' }, m)))
      : h('span', { class: 'pill' }, h('span', { class: 'spin' }), 'connecting')) : null);
}

/** What Claude Code can use on this PC (applied with a restart of its session; the conversation continues). */
function toolsGroup(t) {
  const on = key => t[key] !== false;
  const set = (key, v) => stage('tools', { ...t, [key]: v });
  const chromeLink = h('a', { href: 'https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn',
    target: '_blank', rel: 'noopener',
    onclick: e => { if (SHELL?.openExternal) { e.preventDefault(); SHELL.openExternal(e.currentTarget.href); } } }, 'Claude in Chrome extension');
  return group({ title: 'Using this PC', note: 'These apply when the agent restarts; the conversation continues. Looking (screenshots, reading a page) never asks; acting on the screen or in a browser asks first unless permissions are skipped.' },
    row('Computer', toggle(on('computer'), v => set('computer', v), 'Computer'),
      'See the screen and use the mouse and keyboard, for desktop apps.'),
    row('Browser', toggle(on('browser'), v => set('browser', v), 'Browser'),
      'A Chrome window of its own for web pages (its sign-ins are kept separately from yours).'),
    row('Your Chrome', toggle(on('chrome'), v => set('chrome', v), 'Your Chrome'),
      h('span', {}, 'Works in your own Chrome, signed in as you. Needs the ', chromeLink, ' in Chrome.')));
}

/** Compacting replaces the earlier conversation with a summary, so a long one can go on. */
function compactRow(b, backend) {
  const live = backend.capabilities.includes('context');            // it says how full the context is
  const at = b.compact_at ?? 80;
  const marks = [0, 70, 80, 90].includes(at) ? [70, 80, 90] : [70, 80, 90, at].sort((x, y) => x - y);
  const when = live ? select([['0', 'Leave it to the agent'], ...marks.map(n => [String(n), `When ${n}% full`])], String(at),
    val => save(['backend', 'compact_at'], Number(val))) : null;
  const now = btn('Compact now', () => send('/api/session/compact')
    .then(() => toast('Compacting the conversation. The activity log says how it went.')).catch(e => toast(e.message)), { kind: 'sm' });
  return row('Compact the conversation', h('div', { class: 'ctlpair' }, when, now),
    (live ? 'Replaces the earlier conversation with a summary, so a long one can go on. Done while the agent waits for you, ' +
      `before ${backend.label} would stop to do it in the middle of a reply.`
      : `${backend.label} does this on its own as the conversation nears the model's limit.`) +
    ' When a prompt fails because the conversation is too long, it is compacted and the prompt asked again.');
}

function agentTab() {
  const { config: c, backend, backends } = view;
  const b = c.backend;
  // the settings applied with a restart show as they will be (pick); the rest are saved as they change
  const type = pick('type'), cwd = pick('cwd'), skip = pick('skip_permissions');
  const switching = type !== b.type, label = agentLabel(type);

  const folder = SHELL
    ? h('div', { class: 'pathrow' }, icon(ICONS.folder), h('code', { title: cwd }, cwd), btn('Choose...', async () => {
        const dir = await SHELL.panelChooseFolder(cwd);
        if (dir) stage('cwd', dir);
      }, { kind: 'sm' }))
    : text(cwd, val => { if (val) stage('cwd', val); });

  const skipRow = h('div', { class: `field danger-field ${skip ? 'is-on' : ''}`, id: 'skip-row' },
    h('div', { class: 'flabel' }, h('span', { class: 'ftitle' }, 'Skip all permissions'),
      h('small', {}, `${label} acts without asking first: it can run any command and change any file in the working folder (and beyond). Only use it in folders you trust.`)),
    h('div', { class: 'fctl' }, toggle(skip || confirming, on => {
      if (on) { confirming = true; render(); } else { confirming = false; stage('skip_permissions', false); }
    }, 'Skip all permissions', !backend.capabilities.includes('skip_permissions'))));
  const confirm = confirming && !skip ? h('div', { class: 'confirm', role: 'alertdialog' },
    icon(ICONS.shield),
    h('div', {},
      h('p', {}, `Turn on skipping? When you apply, ${label} restarts (your conversation continues) and from then on never asks before acting. While it is on, the panel's header shows a red warning.`),
      h('div', { class: 'row' },
        btn('Cancel', () => { confirming = false; render(); }, { kind: 'sm' }),
        btn('Turn on', () => { confirming = false; stage('skip_permissions', true); }, { kind: 'danger sm' })))) : null;

  const groups = [
    group({ title: 'Agent' },
      row('Agent', backends.length > 1
        ? select(backends.map(x => [x.type, x.label]), type, val => stage('type', val))
        : h('span', { class: 'static' }, backend.label),
      backends.length > 1 ? 'Each agent keeps its own conversation, so switching back picks it up again.' : null),
      row('Model', modelPicker(type, pick('model'), val => stage('model', val || null),
        { defaultLabel: `Default (${label} decides)`, placeholder: type === 'opencode' ? 'provider/model' : 'Model name' }),
        modelHelp(type, 'The list comes from models.dev and is updated daily.')),
      row('Working folder', folder, 'The agent works here. Changing it starts a new conversation.', { id: 'folder-row', class: 'wide' }),
      row('Start with the face', toggle(b.auto_start, on => save(['backend', 'auto_start'], on), 'Start with the face'),
        'Open the agent session as soon as the face starts, so the face is awake and ready. Off: it sleeps until your first message.'),
      ...(backend.capabilities.includes('compact') ? [compactRow(b, backend)] : [])),
    ...(type === 'claude' ? [toolsGroup(pick('tools') || {})] : []),
    group({ title: 'Permissions' },
      row('When to ask', select(Object.entries(backend.permission_modes), b.permission_mode,
        val => save(['backend', 'permission_mode'], val), skip || switching),
        switching ? `Choose this after switching to ${label}.` : skip ? 'Not used while permissions are skipped.' : backend.permission_help),
      skipRow, confirm),
    roomGroup(c, view.room),
    phoneGroup(c),
    group({ title: 'This face', note: 'Each face has its own file, so faces can run side by side with different settings.' },
      row('Name', text(c.name, val => save(['name'], val)), 'What it is called, on screen and when you say its name.'),
      row('Profile', h('span', { class: 'static' }, view.face)),
      row('Config file', h('div', { class: 'pathrow' }, h('code', { title: view.file }, view.file),
        h('button', { class: 'iconbtn', title: 'Copy the path', 'aria-label': 'Copy the path',
          onclick: () => navigator.clipboard?.writeText(view.file).then(() => toast('Path copied.')) }, icon(ICONS.copy))), null, { class: 'wide' })),
  ];
  // while the agent restarts, nothing here can be changed: the tab shows what it is restarting with
  return [...(applying ? [h('div', { class: 'locked', inert: true }, groups)] : groups), applyBar()];
}

// --- drawing ------------------------------------------------------------------------------------------------------------
function render() {
  if (!view) return;
  const [voice, agent] = roots();
  keepFocus(voice, () => voice.replaceChildren(h('div', { class: 'page' }, voiceTab())));
  keepFocus(agent, () => agent.replaceChildren(h('div', { class: 'page' }, agentTab())));
}
