// DeepFlux Room discovery — the "phone book", never the "post office".
//
// The community chat (ircmgr/room.py) is hosted by the first user online:
// their app runs the chat server, everyone else connects to it directly.
// The only thing this endpoint stores is a POINTER to the current host
// (endpoints + timestamp + host token, sealed by the room key in setup
// builds). No chat content, no nicknames, nothing else — ever.
//
// Semantics (POST {action, room, token, blob}):
//   announce — claim the slot for this host. If a different live host holds
//              it, answer {ok, taken, blob} so the loser can join instead.
//   refresh  — renew OUR slot (host heartbeat every 30s; TTL 120s).
//              {ok:false, taken:true} tells the host it was demoted.
//   leave    — delete our slot on graceful shutdown (best effort; the TTL
//              cleans up crashed hosts anyway).
// GET ?room=<id>  → {ok, blob|null}
// GET ?mode=myip  → {ok, ip} — lets a host learn its public address.
//
// Without the Upstash env vars every action answers {ok:false} — the app
// then hosts LAN-only / direct-address and stays fully functional.

const PREFIX = 'df:room:';
const TTL_S = 120;

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

function key(room) {
  return PREFIX + room;
}

// Stored value is JSON {token, blob}; only the blob ever leaves the server.
function parseEntry(raw) {
  if (typeof raw !== 'string' || !raw) return null;
  try {
    const entry = JSON.parse(raw);
    if (entry && typeof entry.token === 'string' && typeof entry.blob === 'string') {
      return entry;
    }
  } catch (e) { /* fallthrough */ }
  return null;
}

function sanitized(field, max) {
  return typeof field === 'string' ? field.replace(/[^a-zA-Z0-9._:-]/g, '').slice(0, max) : '';
}

module.exports = async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Headers', 'content-type');
  res.setHeader('Cache-Control', 'no-store');
  if (req.method === 'OPTIONS') return res.status(204).end();

  if (!creds()) return res.status(200).json({ ok: false, reason: 'not-configured' });

  try {
    if (req.method === 'GET') {
      if (req.query && req.query.mode === 'myip') {
        const fwd = String(req.headers['x-forwarded-for'] || '');
        const ip = fwd.split(',')[0].trim() || req.socket.remoteAddress || '';
        return res.status(200).json({ ok: true, ip });
      }
      const room = sanitized(req.query && req.query.room, 32);
      if (!room) return res.status(400).json({ ok: false });
      const out = await upstash([['get', key(room)]]);
      const entry = out && out[0] && parseEntry(out[0].result);
      // A GET does not extend the TTL — only the host's refreshes do.
      return res.status(200).json({ ok: true, blob: entry ? entry.blob : null });
    }

    if (req.method !== 'POST') return res.status(405).json({ ok: false });

    const body = typeof req.body === 'object' && req.body ? req.body : JSON.parse(req.body || '{}');
    const action = String(body.action || '');
    const room = sanitized(body.room, 32);
    const token = sanitized(body.token, 64);
    const blob = typeof body.blob === 'string' ? body.blob.slice(0, 4096) : '';
    if (!room) return res.status(400).json({ ok: false });

    const k = key(room);
    if (action === 'announce') {
      if (!blob || !token) return res.status(400).json({ ok: false });
      // SET NX is atomic: a live foreign host's slot is never clobbered.
      const out = await upstash([
        ['set', k, JSON.stringify({ token, blob }), 'EX', TTL_S, 'NX'],
        ['get', k],
      ]);
      const setResult = out && out[0] && out[0].result;
      const entry = out && out[1] && parseEntry(out[1].result);
      if (setResult === 'OK') return res.status(200).json({ ok: true });
      if (entry && entry.token === token) {
        // our own live slot (re-claim after a quick restart) — renew it
        await upstash([['set', k, JSON.stringify({ token, blob }), 'EX', TTL_S]]);
        return res.status(200).json({ ok: true });
      }
      return res.status(200).json({ ok: true, taken: true, blob: entry ? entry.blob : null });
    }
    if (action === 'refresh') {
      if (!blob || !token) return res.status(400).json({ ok: false });
      const out = await upstash([['get', k]]);
      const entry = out && out[0] && parseEntry(out[0].result);
      if (!entry) return res.status(200).json({ ok: false, gone: true });
      if (entry.token !== token) return res.status(200).json({ ok: false, taken: true });
      await upstash([['set', k, JSON.stringify({ token, blob }), 'EX', TTL_S]]);
      return res.status(200).json({ ok: true });
    }
    if (action === 'leave') {
      if (!token) return res.status(400).json({ ok: false });
      const out = await upstash([['get', k]]);
      const entry = out && out[0] && parseEntry(out[0].result);
      if (entry && entry.token === token) {
        await upstash([['del', k]]);
      }
      return res.status(200).json({ ok: true });
    }
    return res.status(400).json({ ok: false });
  } catch (e) {
    // The app must never care — answer 200 and move on.
    return res.status(200).json({ ok: false });
  }
};
