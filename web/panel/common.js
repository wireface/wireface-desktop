// Shared by the panel's pages: the token, the API, and a tiny DOM builder.
// Everything shown comes from the agent (tool output, file names, commands), so it is only ever put in the page as
// text (textContent / text nodes), never as HTML.
const hash = new URLSearchParams(location.hash.slice(1));
export const TOKEN = hash.get('t') || sessionStorage.getItem('faceToken') || '';
if (hash.get('t')) sessionStorage.setItem('faceToken', TOKEN);
export const SHELL = window.faceShell || null;
export const $ = id => document.getElementById(id);

export async function send(path, method = 'GET', body) {
  const r = await fetch(path, {
    method, headers: { 'X-Face-Token': TOKEN, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}

/** h('div', { class: 'x', onclick: fn, dataset: { a: 1 }, hidden: true }, 'text', childElement, null) */
export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'text') el.textContent = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return '';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}
export const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
export const fmtCost = usd => (usd === null || usd === undefined ? '' : usd < 0.01 ? '<$0.01' : `$${usd.toFixed(2)}`);
export const fmtTokens = n => (n >= 10000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
