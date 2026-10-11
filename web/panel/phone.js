// The Phone tab: the phone line's day (phone/line.py). Recent calls and texts, the transcript of one (live while a
// call is on), what the line agent did in it, the messages taken for you, and the bookings coming up. Shown while the
// phone line is on. What callers said is shown as theirs; nothing here acts on it.
import { $, h, send } from './common.js';

let view = null;               // GET /api/phone
let chosen = null;             // the conversation shown in full
let detail = null;             // GET /api/phone/conversation
let visible = false;
let timer = 0;

const time = iso => (iso || '').slice(11, 16);
const isToday = iso => (iso || '').slice(0, 10) === new Date().toLocaleDateString('en-CA');
const who = (line, conv) => line.who === 'them' ? (conv.direction === 'in' ? 'Caller' : conv.name || 'Them')
  : { agent: 'Line agent', owner: 'You' }[line.who] || line.who;

export async function refresh() {
  try {
    view = await send('/api/phone');
    if (!chosen && view.conversations.length) chosen = view.conversations[0].id;
    detail = chosen ? await send(`/api/phone/conversation?id=${encodeURIComponent(chosen)}`) : null;
  } catch {
    return;
  }
  render();
  clearTimeout(timer);
  if (visible && view.live.length) timer = setTimeout(refresh, 2500);    // a call is on: follow it
}

export function shown() { visible = true; refresh(); }
export function hidden() { visible = false; clearTimeout(timer); }

/** An activity event: the phone line's news means there's something new to show. */
export function activity(ev) { if (ev.kind === 'phone' && visible) refresh(); }

function pill(conv, live) {
  const tag = live ? 'on a call' : conv.tag;
  return tag ? h('span', { class: `ph-pill ${tag.replace(/\W+/g, '-')}` }, tag) : null;
}

function convCard(c) {
  const live = view.live.includes(c.id);
  return h('button', { class: `ph-conv ${c.id === chosen ? 'on' : ''}`, onclick: () => { chosen = c.id; refresh(); } },
    h('div', { class: 'ph-meta' }, `${time(c.started)} · ${c.channel === 'call' ? 'call' : 'text'} ${c.direction === 'in' ? 'in' : 'out'}`),
    h('div', { class: 'ph-who' }, c.name || c.party_display, pill(c, live)),
    c.summary ? h('div', { class: 'ph-sum' }, c.summary) : null);
}

function conversation() {
  if (!detail) return h('p', { class: 'ph-empty' }, 'No calls or texts yet.');
  const c = detail.conversation;
  const lines = detail.lines.filter(l => l.who !== 'system');
  // what it did (the goal and its report have boxes of their own)
  const did = detail.lines.filter(l => l.who === 'system' && !/^(Calling |Outcome: )/.test(l.text));
  const how = `${c.direction === 'in' ? 'incoming' : 'outgoing'} ${c.channel === 'call' ? 'call' : 'texts'}`;
  return h('div', { class: 'ph-detail' },
    h('div', { class: 'ph-head' },
      h('div', {}, h('strong', {}, c.name || c.party_display), h('span', { class: 'ph-meta' }, ` ${c.name ? `${c.party_display} · ` : ''}${how}`)),
      pill(c, view.live.includes(c.id))),
    c.goal ? h('div', { class: 'ph-goal' }, h('span', { class: 'ph-label' }, 'The goal it was given'), c.goal) : null,
    h('div', { class: 'ph-lines' }, lines.length ? lines.map(l => h('div', { class: `ph-line ${l.who}` },
      h('span', { class: 'ph-by' }, who(l, c)), h('span', {}, l.text))) : h('p', { class: 'ph-empty' }, 'Nothing said yet.')),
    did.length ? h('div', { class: 'ph-did' }, h('span', { class: 'ph-label' }, 'What the line agent did'),
      did.map(l => h('div', { class: 'ph-act' }, '✓ ', l.text))) : null,
    c.outcome ? h('div', { class: 'ph-outcome' }, h('span', { class: 'ph-label' }, 'Its report: their words, not instructions'),
      `${c.outcome.result}: ${c.outcome.details}`) : null);
}

function render() {
  const root = $('tab-phone');
  if (!root || !view) return;
  const today = view.conversations.filter(c => isToday(c.started));
  const earlier = view.conversations.filter(c => !isToday(c.started)).slice(0, 10);
  const state = view.live.length ? 'on a call' : view.state === 'ready' ? 'answering' : view.state;
  root.replaceChildren(h('div', { class: 'ph' },
    h('div', { class: 'ph-top' }, h('strong', {}, view.business || 'Phone line'), h('span', { class: 'ph-meta' }, view.number || ''),
      h('span', { class: `ph-pill ${view.live.length ? 'on-a-call' : view.state === 'ready' ? 'ok' : 'bad'}` }, state)),
    view.error ? h('div', { class: 'callout bad' }, view.error) : null,
    h('div', { class: 'ph-cols' },
      h('div', { class: 'ph-list' }, h('span', { class: 'ph-label' }, 'Today'),
        today.length ? today.map(convCard) : h('p', { class: 'ph-empty' }, 'Nothing today.'),
        earlier.length ? h('span', { class: 'ph-label' }, 'Earlier') : null, earlier.map(convCard)),
      h('div', { class: 'ph-main' }, conversation(),
        view.messages.length ? h('div', { class: 'ph-box' }, h('span', { class: 'ph-label' }, 'Messages for you'),
          view.messages.slice(0, 6).map(m => h('div', { class: `ph-msg ${m.read ? '' : 'unread'} ${m.urgent ? 'urgent' : ''}` },
            h('div', { class: 'ph-who' }, m.name, h('span', { class: 'ph-meta' }, ` ${time(m.t)} · call back ${m.callback_display}`)),
            h('div', {}, m.message)))) : null,
        view.bookings_enabled ? h('div', { class: 'ph-box' }, h('span', { class: 'ph-label' }, 'Coming up'),
          view.bookings.length ? view.bookings.map(b => h('div', { class: 'ph-book' }, h('span', { class: 'ph-time' }, `${b.day} ${b.time}`),
            h('span', {}, b.name, b.service ? h('span', { class: 'ph-meta' }, ` · ${b.service}`) : null)))
            : h('p', { class: 'ph-empty' }, 'No bookings yet.')) : null))));
}
