const express = require('express');

const router = express.Router();
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const UPSTREAM_URL =
  process.env.NEWSLETTER_UPSTREAM_URL ||
  'https://athena.tail3f9a13.ts.net/newsletter/subscribe';

/**
 * POST /newsletter/subscribe
 * Optional proxy (unused in production: homepage uses Apps Script, Athena sends mail).
 */
router.post('/subscribe', async (req, res) => {
  const email = String(req.body?.email || '').trim().toLowerCase();
  const name = String(req.body?.name || '').trim();
  const source = String(req.body?.source || 'homepage').trim() || 'homepage';
  // Honeypot: bots that fill website/company/url are rejected silently
  const honeypot = String(
    req.body?.website || req.body?.company || req.body?.url || req.body?.hp_website || ''
  ).trim();
  if (honeypot) {
    return res.status(200).json({ ok: true, created: false, status: 'ignored' });
  }

  if (!EMAIL_RE.test(email)) {
    return res.status(400).json({ error: 'Invalid email address' });
  }

  const allowedSources = ['homepage', 'checkout', 'import'];
  if (!allowedSources.includes(source)) {
    return res.status(400).json({ error: 'Invalid source' });
  }

  try {
    const upstream = await fetch(UPSTREAM_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, name, source, website: '' }),
    });
    const data = await upstream.json().catch(() => ({}));
    return res.status(upstream.status).json(data.ok === false ? data : data);
  } catch (err) {
    console.error('Newsletter upstream proxy failed', err);
    return res.status(502).json({
      error: 'Subscribe service unavailable. Try again shortly.',
    });
  }
});

module.exports = router;
