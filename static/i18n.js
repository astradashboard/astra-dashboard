// Traduction de l'interface (français → anglais, allemand, espagnol).
// Le serveur injecte window.ASTRA_LANG et window.I18N (dictionnaire « texte français → texte traduit » de la langue choisie).
(function () {
  const LANG = window.ASTRA_LANG || 'fr';
  const D = window.I18N || {};
  window.LANG = LANG;
  window.LOC = { fr: 'fr-BE', en: 'en-GB', de: 'de-DE', es: 'es-ES' }[LANG] || 'fr-BE';
  const on = LANG !== 'fr' && Object.keys(D).length > 0;

  // morceaux de phrases (textes construits par concaténation) : remplacés du plus long au plus court
  let FRAG = null;
  if (on) {
    const keys = Object.keys(D).filter(k => k.trim().length >= 4 && /\p{L}{2}/u.test(k) && (/\s/.test(k.trim()) || k.trim().length >= 6 || /^\s|\s$/.test(k)))
      .sort((a, b) => b.length - a.length);
    const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const pat = k => (/^\p{L}/u.test(k) ? '(?<!\\p{L})' : '') + esc(k) + (/\p{L}$/u.test(k) ? '(?!\\p{L})' : '');
    if (keys.length) FRAG = new RegExp(keys.map(pat).join('|'), 'gu');
  }
  const cache = new Map();
  function trAll(s) {
    if (!on || typeof s !== 'string') return s;
    if (cache.has(s)) return cache.get(s);
    let out = s;
    const t = s.trim();
    if (t && D[t] !== undefined) out = s.replace(t, D[t]);
    else if (t.length >= 3 && /\p{L}{2}/u.test(t) && FRAG) out = s.replace(FRAG, m => D[m] !== undefined ? D[m] : m);
    if (cache.size > 20000) cache.clear();
    cache.set(s, out);
    return out;
  }
  // traduction exacte d'un texte (sans morceaux), pour le code
  window.tr = s => (on && typeof s === 'string' && D[s] !== undefined) ? D[s] : (on ? trAll(s) : s);
  window.trAll = trAll;
  if (!on) return;

  const ATTRS = ['title', 'placeholder', 'aria-label', 'data-tip', 'alt'];
  const done = new WeakMap();   // nœud texte → dernière valeur écrite par nous
  const skip = n => { for (let e = n.nodeType === 1 ? n : n.parentElement; e; e = e.parentElement) { if (e.hasAttribute && e.hasAttribute('data-noi18n')) return true; const tg = e.tagName; if (tg === 'SCRIPT' || tg === 'STYLE' || tg === 'TEXTAREA') return true; } return false; };
  function doText(n) {
    const v = n.data;
    if (done.get(n) === v || v.length < 2 || !/\p{L}/u.test(v)) return;
    if (skip(n)) return;
    const o = trAll(v);
    done.set(n, o);
    if (o !== v) n.data = o;
  }
  function doAttrs(e) {
    if (skip(e)) return;
    for (const a of ATTRS) {
      const v = e.getAttribute(a); if (!v) continue;
      const o = trAll(v); if (o !== v) e.setAttribute(a, o);
    }
    if (e.tagName === 'INPUT' && (e.type === 'button' || e.type === 'submit') && e.value) { const o = trAll(e.value); if (o !== e.value) e.value = o; }
  }
  function walk(root) {
    if (root.nodeType === 3) return doText(root);
    if (root.nodeType !== 1) return;
    if (root.hasAttribute('data-noi18n')) return;
    doAttrs(root);
    const it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: n => n.nodeType === 1 && (n.hasAttribute('data-noi18n') || n.tagName === 'SCRIPT' || n.tagName === 'STYLE') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    let n; while ((n = it.nextNode())) n.nodeType === 3 ? doText(n) : doAttrs(n);
  }
  window.i18nWalk = walk;
  const mo = new MutationObserver(list => {
    for (const m of list) {
      if (m.type === 'childList') m.addedNodes.forEach(walk);
      else if (m.type === 'characterData') doText(m.target);
      else if (m.type === 'attributes') { const v = m.target.getAttribute(m.attributeName); if (v) { const o = trAll(v); if (o !== v && !skip(m.target)) m.target.setAttribute(m.attributeName, o); } }
    }
  });
  function start() {
    walk(document.body);
    mo.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }
  if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);

  // textes dessinés sur les cartes de résultats (canvas)
  const C = CanvasRenderingContext2D.prototype, fT = C.fillText, mT = C.measureText, sT = C.strokeText;
  C.fillText = function (t, ...a) { return fT.call(this, typeof t === 'string' ? trAll(t) : t, ...a); };
  C.strokeText = function (t, ...a) { return sT.call(this, typeof t === 'string' ? trAll(t) : t, ...a); };
  C.measureText = function (t) { return mT.call(this, typeof t === 'string' ? trAll(t) : t); };
  // boîtes de dialogue natives
  const cf = window.confirm, al = window.alert;
  window.confirm = m => cf.call(window, trAll(String(m)));
  window.alert = m => al.call(window, trAll(String(m)));
})();
