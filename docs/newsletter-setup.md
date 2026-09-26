# Newsletter signup + automated email chain

Homepage signup writes **KA Inventory → Newsletter**. Athena sends the drip over Migadu SMTP. No Tailscale Funnel, no AWS.

## Architecture

```
┌─────────────────┐   POST Apps Script /exec     ┌─────────────────────────────┐
│  kommu.ai       │ ───────────────────────────► │ KA Inventory → Newsletter   │
│  (homepage)     │                              └──────────────┬──────────────┘
└─────────────────┘                                             │
                                                                │ every 1m
┌─────────────────┐   every 30m sync_orders.py                  ▼
│  Orders tab     │ ───────────────────────────►          Athena run.py
└─────────────────┘                              ┌─────────────────────────────┐
                                                 │ Migadu SMTP                 │
                                                 │ email 1: as soon as queued  │
                                                 │ email 2–6: 08:00 MYT,       │
                                                 │   every other day           │
                                                 └─────────────────────────────┘
```

## 1. Google Sheet — KA Inventory → `Newsletter` tab

Row 1 headers (exact order):

| Column | Description |
|--------|-------------|
| `email` | Primary key (lowercase) |
| `name` | Display name |
| `source` | `homepage`, `checkout`, or `import` |
| `subscribed_at` | ISO8601 UTC when first added |
| `sequence_step` | `0` = none sent yet; `1`–`6` = last step sent |
| `last_sent_at` | ISO8601 UTC of last drip email |
| `status` | `active`, `completed`, or `unsubscribed` |
| `next_send_at` | Optional; runner can leave blank |

Share **KA Inventory** with your Google service account (Editor).

## 2. Email sequence

Defined in [`_data/newsletter_sequence.yaml`](../_data/newsletter_sequence.yaml). Edit copy in [`tools/newsletter-runner/templates/`](../tools/newsletter-runner/templates/).

## 3. Frontend (kommuweb)

- [`_includes/newsletter_signup.html`](../_includes/newsletter_signup.html) on `index.html`
- API URL from [`_config.yml`](../_config.yml) → `newsletter_api_url` (Apps Script `/exec`)
- **Honeypot:** hidden `website` field on the form. Bots that fill it get a fake success; Apps Script / Athena / proxy reject without writing a row. After changing [`newsletter-subscribe-api.gs`](scripts/newsletter-subscribe-api.gs), deploy a **new Apps Script version**.

Paste [`docs/scripts/newsletter-subscribe-api.gs`](scripts/newsletter-subscribe-api.gs) into **KA Inventory** → Extensions → Apps Script → Deploy → Web app (Anyone).

```yaml
newsletter_api_url: "https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec"
```

Athena never serves that form. It only reads the Newsletter tab and sends SMTP.

## 4. Athena — drip + order sync

Location: [`tools/newsletter-runner/`](../tools/newsletter-runner/)

```bash
rsync -az --exclude .venv --exclude logs --exclude .env \
  tools/newsletter-runner/ kommu@ATHENA:/data/kommu/newsletter-runner/
# On Athena
cd /data/kommu/newsletter-runner
./install-athena-newsletter.sh install
```

`install` enables:

| Unit | Role |
|------|------|
| `kommu-newsletter-drip.timer` | Every 1m: send **email 1** as soon as the row is queued (if Migadu quota remains). **Emails 2–6** only during **08:00 MYT**, every other calendar day |
| `kommu-newsletter-sync.timer` | Every 30m: **Orders** tab → **Newsletter** tab |

## 5. Drip emails

Athena `run.py` (Migadu SMTP):

- **Email 1** — next drip tick after signup, usually within a minute, if daily quota remains. Not held until the next hour.
- **Emails 2–6** — **08:00 Malaysia time**, every other day after the previous send. Welcome at 01:00 Monday → next mail Wednesday 08:00, never 01:00 / midnight.

```bash
./install-athena-newsletter.sh test-drip
```

Logs: `logs/newsletter-drip.log`

## 6. Purchasers + import

- **Automatic (PaymentGateway tab):** Apps Script [`docs/scripts/paymentgateway-to-newsletter.gs`](scripts/paymentgateway-to-newsletter.gs) — add as a **new file** in the existing KA Inventory Apps Script project (do not overwrite `Code.gs`), then run `installPaymentGatewayNewsletterTrigger` once. Every 10 minutes it copies **only newly appended** PaymentGateway rows into **Newsletter** (`source=checkout`); no historical backfill, no updates to existing Newsletter rows. Skips emails already present. Requires a header column named like `email` / `customer email`. Reuses `SPREADSHEET_ID` / `NEWSLETTER_TAB` / `HEADERS` / `EMAIL_RE` / `mytNow` from the newsletter API already in that project.
- **Automatic (Orders tab / Athena):** `sync_orders.py` reads **Orders** (`ORDERS_SHEET_TAB`, default `Orders`), upserts with `source=checkout`
- **One-time CSV:** `python import_subscribers.py --csv customers.csv --source import`

## 7. Unsubscribe

Drip emails use `GET ?action=unsubscribe` on the same Apps Script `/exec` URL. Manual fallback: set `status=unsubscribed` in the sheet when users reply or email support@kommu.ai.

## 8. Click tracking

CTA buttons in drip templates have `data-track="button_id"`. Athena rewrites those `href`s to:

`https://kommu.ai/go/?action=click&email=...&token=...&step=...&btn=...&to=...`

[`go.html`](../go.html) logs the click by calling the Apps Script `/exec?action=click&format=json`, then redirects at top level. Apps Script can't redirect by itself: its web-app page runs in a sandboxed Google iframe, and Facebook / App Store / Google Play refuse to load inside it. `/go/` only redirects to hosts in its `ALLOWED_HOSTS` list, so add a host there before using a new CTA domain.

Token is HMAC-SHA256 of `click|{email}|{step}|{btn}|{dest}` with `UNSUBSCRIBE_SECRET` (same secret as unsubscribe). Invalid tokens are not logged. Emails sent before `/go/` still link straight to `/exec`; those show a **Continue** button the reader has to tap.

Clicks append to KA Inventory → **Click log** (created automatically):

| Column | Description |
|--------|-------------|
| `clicked_at` | Malaysia time `dd/MM/yyyy HH:mm:ss` |
| `email` | Subscriber |
| `email_id` | Sequence step id (`kommunity`, `product_features`, …) |
| `button` | `data-track` id (`join_kommunity`, `explore_ka2`, …) |
| `destination` | Original URL |

**This feature needs an Apps Script update.** Paste [`docs/scripts/newsletter-subscribe-api.gs`](scripts/newsletter-subscribe-api.gs) over `Code.gs`, keep `UNSUBSCRIBE_SECRET`, then **Deploy → Manage deployments → pencil → New version**. The 08:00 drip schedule did not require that; click tracking does.

Footer social links are not tracked.

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Welcome took ~1 minute | Athena drip ticks every 60s | Expected; not the old hourly wait |
| Follow-up at midnight | Old runner used signup hour + 2 days | Current runner only sends 2–6 at 08:00 MYT |
| Row missing in sheet | Apps Script / service account permissions | Share KA Inventory with the service account; confirm Apps Script deploy |
| Clicks not logged / buttons go straight to the site | Old Apps Script web-app version | Paste `newsletter-subscribe-api.gs` and deploy a **new version** |
| CTA goes to kommu.ai home instead of the link | Destination host not in `go.html` `ALLOWED_HOSTS` | Add the host and redeploy the site |

## 10. Privacy

Signup requires marketing consent + link to [Privacy Policy](/privacy/).
