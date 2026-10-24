// Relais de vérification des licences ASTRA Dashboard (Cloudflare Worker).
// Il garde la clé API Creem secrète : l'app n'appelle que ce relais.
// Réglage obligatoire : Settings → Variables → secret CREEM_API_KEY (ta clé API Creem).
// Facultatif : variable CREEM_TEST = "1" pour utiliser les clés du mode test.
export default {
  async fetch(request, env) {
    const cors = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type' };
    if (request.method === 'OPTIONS') return new Response(null, { headers: cors });
    const action = new URL(request.url).pathname.replace(/^\/+|\/+$/g, '');
    if (request.method !== 'POST' || !['activate', 'validate', 'deactivate'].includes(action)) {
      return Response.json({ error: 'not found' }, { status: 404, headers: cors });
    }
    let body;
    try { body = await request.json(); } catch { return Response.json({ error: 'bad request' }, { status: 400, headers: cors }); }
    const key = String(body.key || '').slice(0, 200);
    if (!key) return Response.json({ error: 'missing key' }, { status: 400, headers: cors });
    const payload = action === 'activate'
      ? { key, instance_name: String(body.instance_name || 'ASTRA Dashboard').slice(0, 100) }
      : { key, instance_id: String(body.instance_id || '').slice(0, 100) };
    const base = env.CREEM_TEST === '1' ? 'https://test-api.creem.io' : 'https://api.creem.io';
    const r = await fetch(`${base}/v1/licenses/${action}`, {
      method: 'POST',
      headers: { 'x-api-key': env.CREEM_API_KEY, 'Content-Type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(payload),
    });
    return new Response(await r.text(), { status: r.status, headers: { ...cors, 'Content-Type': 'application/json' } });
  },
};
