// Presence heartbeat from the DeepFlux desktop app (infra/telemetry.py).
//
// The app POSTs {id, v, os[, leave]} every 5 minutes while running. The
// counter is a Redis sorted set: member = install id, score = last-seen ms.
// Entries older than 15 minutes are pruned on every write, so "users online"
// = set cardinality. When Upstash env vars are absent the endpoint answers
// 200 {ok:false} — the app treats any answer as fine and stays silent.
//
// Storage: Upstash Redis via the Vercel Marketplace (free tier). The
// integration sets UPSTASH_REDIS_REST_URL / _TOKEN automatically.

const ONLINE_KEY = 'df:online';
const WINDOW_MS = 15 * 60 * 1000;

function creds() {
  return process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN;
}

async function upstash(commands) {
  const r = await fetch(`${process.env.UPSTASH_REDIS_REST_URL}/pipeline`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(commands),
  });
  if (!r.ok) throw new Error(`upstash ${r.status}`);
  return r.json();
}

module.exports = async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'content-type');
  res.setHeader('Cache-Control', 'no-store');
  if (req.method === 'OPTIONS') return res.status(204).end();
  if (req.method !== 'POST') return res.status(405).json({ ok: false });

  if (!creds()) return res.status(200).json({ ok: false, reason: 'not-configured' });

  try {
    const body = typeof req.body === 'object' && req.body ? req.body : JSON.parse(req.body || '{}');
    const id = String(body.id || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 64);
    if (!id) return res.status(400).json({ ok: false, error: 'missing id' });

    const now = Date.now();
    const out = body.leave
      ? await upstash([
          ['zrem', ONLINE_KEY, id],
          ['zcard', ONLINE_KEY],
        ])
      : await upstash([
          ['zadd', ONLINE_KEY, now, id],
          ['zremrangebyscore', ONLINE_KEY, 0, now - WINDOW_MS],
          ['zcard', ONLINE_KEY],
        ]);
    const cardEntry = out && out[out.length - 1];
    const online = cardEntry && typeof cardEntry.result === 'number' ? cardEntry.result : null;
    return res.status(200).json({ ok: true, online });
  } catch (e) {
    // The app must never care — answer 200 and move on.
    return res.status(200).json({ ok: false });
  }
};
