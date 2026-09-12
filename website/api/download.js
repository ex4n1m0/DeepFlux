// Counted setup downloads.
//
// The landing page's Download button points here (?f=DeepFlux<X>Setup.exe).
// This increments df:dl:total (+ a per-file counter) in Upstash and 302s to
// the static file in the same deployment. Direct hits on the static file
// still work (they just aren't counted). Without the Upstash env vars the
// redirect happens uncounted — the download itself must never break.

const FILE_RE = /^DeepFlux[0-9.]+Setup\.exe$/;
const TOTAL_KEY = 'df:dl:total';

function creds() {
  return process.env.UPSTASH_REDIS_REST_URL && process.env.UPSTASH_REDIS_REST_TOKEN;
}

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  const f = String((req.query && req.query.f) || '');
  if (!FILE_RE.test(f)) return res.status(400).send('bad file');

  if (creds()) {
    try {
      await fetch(`${process.env.UPSTASH_REDIS_REST_URL}/pipeline`, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${process.env.UPSTASH_REDIS_REST_TOKEN}`,
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
