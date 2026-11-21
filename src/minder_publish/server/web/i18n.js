/* One UI message catalog, shared with server-rendered system pages. */
let catalog = {};
export async function loadMessages() {
  const response = await fetch('/static/messages.json', { credentials: 'same-origin' });
  if (!response.ok) throw new Error('Could not load interface messages.');
  catalog = await response.json();
}
export function t(key, values = {}) {
  const language = document.documentElement.lang || 'en';
  const message = catalog[key]?.[language] ?? catalog[key]?.en ?? key;
  return message.replace(/\{(\w+)\}/g, (match, name) => values[name] === undefined ? match : String(values[name]));
}
export function translateStatic(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(element => { element.textContent = t(element.dataset.i18n); });
  for (const attribute of ['aria-label', 'placeholder']) {
    root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(element => element.setAttribute(attribute, t(element.getAttribute(`data-i18n-${attribute}`))));
  }
}
// The progressively enhanced picker is a classic script, loaded independently.
window.PublishTranslate = t;
