// Relais de vérification des licences ASTRA Dashboard (Cloudflare Worker).
// Il garde la clé API Creem secrète : l'app n'appelle que ce relais.
// Réglage obligatoire : Settings → Variables → secret CREEM_API_KEY (ta clé API Creem).
// Le mode test est détecté tout seul (clé creem_test_…). Page de diagnostic : /check
// Alertes e-mail des annonces économiques (/notify) : secret RESEND_API_KEY (clé API Resend, domaine astracapital.app vérifié).
// Optionnel : variable MAIL_FROM (par défaut « ASTRA Dashboard <alertes@astracapital.app> »).
export default {
  async fetch(request, env) {
    const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type' };
    if (request.method === 'OPTIONS') return new Response(null, { headers: cors });
    const action = new URL(request.url).pathname.replace(/^\/+|\/+$/g, '');
    const apiKey = String(env.CREEM_API_KEY || '').trim();
    const test = apiKey.startsWith('creem_test_') || String(env.CREEM_TEST || '').trim() === '1';
    const base = test ? 'https://test-api.creem.io' : 'https://api.creem.io';
    if (action === 'check') {
      return Response.json({ cle_api_presente: apiKey.length > 0, debut_cle: apiKey ? apiKey.slice(0, 11) + '…' : null,
        longueur_cle: apiKey.length, mode: test ? 'TEST' : 'REEL', serveur_creem: base }, { headers: cors });
    }
    if (action === 'notify' && request.method === 'POST') return notify(request, env, base, apiKey, cors);
    if (request.method !== 'POST' || !['activate', 'validate', 'deactivate'].includes(action)) {
      return Response.json({ error: 'not found' }, { status: 404, headers: cors });
    }
    let body;
    try { body = await request.json(); } catch { return Response.json({ error: 'bad request' }, { status: 400, headers: cors }); }
    const key = String(body.key || '').trim().slice(0, 200);
    if (!key) return Response.json({ error: 'missing key' }, { status: 400, headers: cors });
    const payload = action === 'activate'
      ? { key, instance_name: String(body.instance_name || 'ASTRA Dashboard').slice(0, 100) }
      : { key, instance_id: String(body.instance_id || '').slice(0, 100) };
    const r = await fetch(`${base}/v1/licenses/${action}`, {
      method: 'POST',
      headers: { 'x-api-key': apiKey, 'Content-Type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(payload),
    });
    return new Response(await r.text(), { status: r.status, headers: { ...cors, 'Content-Type': 'application/json' } });
  },
};

// ---------- alertes e-mail : uniquement pour une clé de licence valide (ou la clé propriétaire) ----------
const OWNER_SHA256 = ['20c8f1e472984770c44f9a0ea7ae360e8d577a460fb38a3e64426e804668d303'];
const FLAGS = { US: '🇺🇸', EU: '🇪🇺', DE: '🇩🇪', FR: '🇫🇷', GB: '🇬🇧', IT: '🇮🇹', ES: '🇪🇸', CH: '🇨🇭', JP: '🇯🇵', CN: '🇨🇳', CA: '🇨🇦', AU: '🇦🇺', NZ: '🇳🇿' };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
async function sha256(t) {
  const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(t));
  return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('');
}
async function notify(request, env, base, apiKey, cors) {
  const fail = (error, status = 400) => Response.json({ error }, { status, headers: cors });
  let body; try { body = await request.json(); } catch { return fail('bad request'); }
  const key = String(body.key || '').trim().slice(0, 200), to = String(body.to || '').trim().slice(0, 200);
  if (!key) return fail('clé de licence manquante', 401);
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(to)) return fail('adresse e-mail invalide');
  const resend = String(env.RESEND_API_KEY || '').trim();
  if (!resend) return fail("l'envoi d'e-mails n'est pas encore configuré sur le relais", 503);
  // licence : propriétaire, sinon vérification auprès de Creem
  if (!OWNER_SHA256.includes(await sha256(key))) {
    const r = await fetch(`${base}/v1/licenses/validate`, { method: 'POST',
      headers: { 'x-api-key': apiKey, 'Content-Type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({ key, instance_id: String(body.instance_id || '').slice(0, 100) }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.status !== 'active') return fail('licence inactive', 402);
  }
  const tz = /^[A-Za-z_]+\/[A-Za-z_\/+-]+$/.test(String(body.tz || '')) ? body.tz : 'Europe/Brussels';
  const events = (Array.isArray(body.events) ? body.events : []).slice(0, 10);
  if (!events.length) return fail('aucune annonce');
  const hour = d => { try { return new Date(d).toLocaleTimeString('fr-BE', { timeZone: tz, hour: '2-digit', minute: '2-digit' }); } catch { return ''; } };
  const val = (v, e) => v === null || v === undefined || v === '' ? '—' : esc(String(v).replace('.', ',') + (e.scale || '') + (e.unit || ''));
  const stars = i => i === 1 ? '★★★' : i === 0 ? '★★☆' : '★☆☆';
  const first = events[0];
  const subject = (body.test ? '[Test] ' : '') + (events.length === 1
    ? `Dans 5 min (${hour(first.date)}) : ${first.title}` : `Dans 5 min (${hour(first.date)}) : ${events.length} annonces économiques`);
  const rows = events.map(e => `<tr>
    <td style="padding:12px 14px;border-top:1px solid #232c3e;white-space:nowrap;color:#e2ab66;font-weight:700">${esc(hour(e.date))}</td>
    <td style="padding:12px 14px;border-top:1px solid #232c3e;white-space:nowrap">${FLAGS[String(e.country || '').toUpperCase()] || ''} ${esc(e.currency || e.country || '')}</td>
    <td style="padding:12px 14px;border-top:1px solid #232c3e"><b style="color:#eef1f6">${esc(e.title)}</b>${e.period ? ` <span style="color:#8f98ab">(${esc(e.period)})</span>` : ''}<br>
      <span style="color:#c48a45;font-size:12px">${stars(e.importance)}</span>
      <span style="color:#8f98ab;font-size:13px">&nbsp; Prévision ${val(e.forecast, e)} · Précédent ${val(e.previous, e)}</span></td></tr>`).join('');
  const html = `<!doctype html><html><body style="margin:0;background:#080b12;font-family:Segoe UI,Arial,sans-serif;color:#eef1f6">
  <div style="max-width:600px;margin:0 auto;padding:28px 18px">
    <div style="font-weight:800;letter-spacing:.16em;font-size:13px;margin-bottom:18px">ASTRA <span style="color:#e2ab66">CAPITAL</span></div>
    <div style="background:#121826;border:1px solid #232c3e;border-radius:16px;overflow:hidden">
      <div style="padding:18px 18px 14px"><div style="color:#e2ab66;font-size:12px;font-weight:700;letter-spacing:.12em">ALERTE CALENDRIER ÉCONOMIQUE</div>
        <div style="font-size:20px;font-weight:800;margin-top:6px">${events.length > 1 ? events.length + ' annonces' : 'Annonce'} dans 5 minutes</div>
        ${body.test ? '<div style="color:#8f98ab;font-size:13px;margin-top:6px">Ceci est un e-mail de test : tes alertes fonctionnent.</div>' : ''}</div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;color:#c9cfdb">${rows}</table>
    </div>
    <p style="color:#697287;font-size:12px;line-height:1.6;margin-top:18px">Heures affichées : ${esc(tz)}. Tu reçois cet e-mail car les alertes sont activées dans ASTRA Dashboard (Calendrier économique → Alertes). Pour les arrêter, désactive-les dans l'application. Information fournie à titre indicatif, sans conseil en investissement.</p>
  </div></body></html>`;
  const text = events.map(e => `${hour(e.date)} ${e.currency || ''} ${e.title} — Prévision ${e.forecast ?? '—'} · Précédent ${e.previous ?? '—'}`).join('\n');
  const r = await fetch('https://api.resend.com/emails', { method: 'POST',
    headers: { Authorization: `Bearer ${resend}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ from: String(env.MAIL_FROM || 'ASTRA Dashboard <alertes@astracapital.app>'), to: [to], subject, html,
      text: `Annonce(s) économique(s) dans 5 minutes :\n${text}\n\nASTRA Dashboard — alertes du calendrier économique.` }) });
  if (!r.ok) return fail('envoi refusé par le service e-mail (' + r.status + ')', 502);
  return Response.json({ ok: true }, { headers: cors });
}
