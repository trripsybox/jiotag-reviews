# JioTag Review Monitor

Scrapes JioTag reviews from Google Play, Apple App Store, and YouTube every
Monday morning. Dedupes, tags with sentiment + topics via Google Gemini (free
tier), appends to a Google Sheet, and produces a downloadable .xlsx file.

Runs entirely on GitHub Actions free tier. No server. No laptop needed. No
email setup. No paid APIs.

## What you'll set up (one-time, ~25 min)

1. GitHub account + repo
2. Google Sheet + service account
3. Gemini API key (free, no credit card)
4. Add 3 secrets to GitHub
5. Push code and test

Follow `SETUP.md` step by step.

## How it runs

Every Monday at 09:30 IST, GitHub Actions runs `scrape.py`. Two things happen:

- Your **Google Sheet** is updated with new reviews (running history).
- A **.xlsx file** (`jiotag_reviews_YYYY-MM-DD.xlsx`) with Summary / New This Run / All Reviews tabs is uploaded as a downloadable artifact, kept for 90 days.

You can also trigger a run manually any time from the GitHub Actions tab → "Run workflow".

## To get the weekly xlsx

Repo → Actions tab → latest run → scroll to Artifacts → click **jiotag-reviews-xlsx**.

## File layout

- `scrape.py` — the scraper
- `requirements.txt` — Python dependencies
- `.github/workflows/weekly.yml` — the cron schedule
- `SETUP.md` — step-by-step setup guide
