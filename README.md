# Regulatory·Watch

A static medical-device regulatory intelligence dashboard that auto-updates every 30 minutes by pulling RSS feeds from official regulatory authorities (FDA, MHRA, Health Canada, TGA, EMA, Federal Register, and more).

**No backend, no database, no API keys, no login.** Just GitHub Pages + GitHub Actions.

---

## How it works

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│ Regulatory feeds     │     │ GitHub Actions       │     │ GitHub Pages         │
│ (RSS / Atom)         │ ──▶ │ runs every 30 min    │ ──▶ │ serves index.html    │
│                      │     │ → writes feed.json   │     │ → fetches feed.json  │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```

1. **`scripts/fetch_feeds.py`** fetches every RSS/Atom feed listed in `FEEDS` and writes a consolidated `data/feed.json`.
2. **`.github/workflows/fetch-feeds.yml`** runs that script on a 30-minute cron, then commits the updated JSON back to the repo.
3. **`.github/workflows/deploy.yml`** publishes the repo to GitHub Pages whenever `main` updates.
4. **`index.html`** loads `data/feed.json` on page load and renders the dashboard.

---

## Setup (5 minutes)

### 1. Push to GitHub

```bash
cd regulatory-watch
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/regulatory-watch.git
git push -u origin main
```

### 2. Enable GitHub Pages

- Go to **Settings → Pages**
- Under **Source**, choose **GitHub Actions**

### 3. Enable Actions write permissions

- Go to **Settings → Actions → General**
- Under **Workflow permissions**, choose **Read and write permissions**
- Save

### 4. Trigger first run

- Go to **Actions → Fetch regulatory feeds → Run workflow**
- After it succeeds, your site is live at `https://YOUR_USERNAME.github.io/regulatory-watch`

The site will refresh every 30 minutes from then on.

---

## Adding feeds

Edit `scripts/fetch_feeds.py` and add to the `FEEDS` list:

```python
{
    "agency": "PMDA",                 # short code shown in the UI
    "region": "Japan",                # friendly label
    "category": "postmarket",         # premarket | postmarket | guidance | recall | prepub
    "name": "PMDA Notifications",     # source label
    "url": "https://....rss",         # feed URL
},
```

Commit and push. The next scheduled run picks it up automatically.

---

## Files

```
regulatory-watch/
├── index.html                          # The dashboard UI
├── data/
│   └── feed.json                       # Auto-generated, refreshed every 30 min
├── scripts/
│   └── fetch_feeds.py                  # Feed-fetcher script
└── .github/
    └── workflows/
        ├── fetch-feeds.yml             # Cron job for feed updates
        └── deploy.yml                  # Publishes to GitHub Pages
```

---

## Notes & limits

- **GitHub Actions free tier**: 2,000 minutes/month for private repos, unlimited for public repos. A feed-fetch run takes ~30 seconds, so you'll burn ~24 minutes/day at the 30-minute cadence — well within the public-repo allowance and fine even for a private repo.
- **Cron schedule reliability**: GitHub Actions cron is best-effort and can be delayed during high load. For critical timing, run more frequently (e.g., every 15 min) so a delay still produces fresh data.
- **Feed availability**: Some emerging-market regulators (NMPA, CDSCO, ANVISA) don't publish RSS feeds. Adding them requires a separate scraper or paid intelligence service.
- **Sample data**: Until the first scheduled run, `data/feed.json` ships with sample content so the dashboard renders something on day one.
