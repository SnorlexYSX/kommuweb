# Kommu newsletter runner (Athena)

Homepage signup writes the **KA Inventory → Newsletter** tab (Google Apps Script). **Athena** reads that sheet and sends mail over Migadu SMTP. No Tailscale Funnel, no AWS.

- **Drip sender** — every 1 minute: send welcome (step 1) as soon as the row appears and quota remains; emails 2–6 only at **08:00 MYT**, every other day
- **Click tracking** — CTA `data-track` links rewrite to `kommu.ai/go/`, which logs to Apps Script (KA Inventory → **Click log**) then redirects
- **Order sync** — every 30m copies purchaser emails from **Orders** tab

See [docs/newsletter-setup.md](../../docs/newsletter-setup.md).

## Quick start on Athena

```bash
cd /data/kommu/newsletter-runner
cp config.example.env .env         # KA Inventory + SMTP + service account
chmod +x install-athena-newsletter.sh
./install-athena-newsletter.sh install
```

Homepage `_config.yml` already points at the Apps Script web app, not Athena.

## Commands

```bash
./install-athena-newsletter.sh status
./install-athena-newsletter.sh test-drip
./install-athena-newsletter.sh test-sync
python import_subscribers.py --csv customers.csv --source import --dry-run
```

## Sync from Mac

```bash
rsync -az --exclude .venv --exclude logs --exclude .env \
  tools/newsletter-runner/ kommu@192.168.0.80:/data/kommu/newsletter-runner/
```
