// Counted setup downloads.
//
// The landing page's Download buttons point here (?f=DeepFlUX<X>Setup.exe
// for Windows, ?f=DeepFlux-<X>-Linux-x86_64.AppImage for Linux). This
// increments df:dl:total (+ a per-file counter) in Upstash and 302s to the
// static file in the same deployment. Direct hits on the static file still
// work (they just aren't counted). Without the Upstash env vars the
// redirect happens uncounted — the download itself must never break.
// (The retired macOS dmg pattern was dropped 2026-11-06 — macOS left the
// site on 2026-09-24 and its artifacts are gone; keep it out of FILE_RE.)

const FILE_RE = /^DeepFlux(?:[0-9.]+Setup\.exe|-[0-9.]+-Linux-x86_64\.(?:AppImage|tar\.gz))$/;
const TOTAL_KEY = 'df:dl:total';

// Accept both naming schemes: UPSTASH_REDIS_REST_* (a manual Upstash token)
// and KV_REST_API_* (what the Vercel Marketplace integration sets).
const REST_URL = () => process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
const REST_TOKEN = () => process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;

// The REST URL is operator config, but pin it to real Upstash hosts anyway
// (every Upstash REST endpoint is https://<hash>.upstash.io) so a poisoned
// env var can never turn this function into an arbitrary request proxy.
// Anything else degrades to the documented not-configured path.
function redisUrl() {
  const u = REST_URL();
  try {
    const p = new URL(u);
    if (p.protocol === 'https:' && p.hostname.endsWith('.upstash.io')) return u;
  } catch (e) { /* fallthrough */ }
  return '';
}

function creds() {
  return redisUrl() && REST_TOKEN();
}

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  const f = String((req.query && req.query.f) || '');
  if (!FILE_RE.test(f)) return res.status(400).send('bad file');

  if (creds()) {
    try {
      await fetch(`${redisUrl()}/pipeline`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${REST_TOKEN()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify([
          ['incr', TOTAL_KEY],
          ['incr', `df:dl:${f}`],
        ]),
      });
    } catch (e) {
      // Counting is best-effort; fall through to the redirect.
    }
  }
  res.redirect(302, `/deepflux/${encodeURIComponent(f)}`);
};
