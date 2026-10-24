// Relais de vérification des licences ASTRA Dashboard (Cloudflare Worker).
// Il garde la clé API Creem secrète : l'app n'appelle que ce relais.
// Réglage obligatoire : Settings → Variables → secret CREEM_API_KEY (ta clé API Creem).
// Le mode test est détecté tout seul (clé creem_test_…). Page de diagnostic : /check
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
