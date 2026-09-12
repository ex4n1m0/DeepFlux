// Public stats for the landing page: {online, downloads}.
//
// "online"  — installs whose heartbeat arrived in the last 15 minutes.
// "downloads" — lifetime clicks on /api/download (total counter).
// Both are null while the Upstash env vars are missing — the page hides the
// widgets in that case instead of showing zeros.

const ONLINE_KEY = 'df:online';
const TOTAL_KEY = 'df:dl:total';
const WINDOW_MS = 15 * 60 * 1000;

// Accept both naming schemes: UPSTASH_REDIS_REST_* (a manual Upstash token)
// and KV_REST_API_* (what the Vercel Marketplace integration sets).
const REST_URL = () => process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
const REST_TOKEN = () => process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;

function creds() {
  return REST_URL() && REST_TOKEN();
}

async function upstash(commands) {
  const r = await fetch(`${REST_URL()}/pipeline`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${REST_TOKEN()}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(commands),
  });
  if (!r.ok) throw new Error(`upstash ${r.status}`);
  return r.json();
}

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  if (!creds()) return res.status(200).json({ online: null, downloads: null });

  try {
    const now = Date.now();
    const out = await upstash([
      ['zremrangebyscore', ONLINE_KEY, 0, now - WINDOW_MS],
      ['zcard', ONLINE_KEY],
      ['get', TOTAL_KEY],
    ]);
    const num = (i) => (out && out[i] && typeof out[i].result === 'number' ? out[i].result : null);
    const str = (i) => (out && out[i] && typeof out[i].result === 'string' ? parseInt(out[i].result, 10) : null);
    return res.status(200).json({ online: num(1), downloads: str(2) });
  } catch (e) {
    return res.status(200).json({ online: null, downloads: null });
  }
};
