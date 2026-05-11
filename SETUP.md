# Setup guide

Do these in order. Most steps are clicking through web UIs. Total time: ~25 min of setup + ~25 min for the first test run to finish.

Sources covered: Google Play Store, Apple App Store, YouTube comments.
LLM for sentiment tagging: Gemini (free tier, no credit card).
Output: Google Sheet (continuously updated) + downloadable .xlsx file (one per weekly run).

---

## Step 1 — Create a GitHub account and repo (5 min)

1. Go to **https://github.com/signup** and sign up (free).
2. Verify your email.
3. Click the **+** icon top-right → **New repository**.
4. Name: `jiotag-reviews`. Set to **Private**. Don't check any "initialize" boxes. Click **Create repository**.
5. Leave this tab open — you'll come back to it.

---

## Step 2 — Create a Google Sheet (3 min)

1. Go to **https://sheets.google.com** → blank sheet.
2. Name it `JioTag Reviews`.
3. Copy the **Sheet ID** from the URL — it's the long string between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART_IS_THE_ID`**`/edit`
4. Save the Sheet ID somewhere — you'll paste it into GitHub later.

---

## Step 3 — Create a Google service account (10 min)

This is the only fiddly step. Take it slow.

1. Go to **https://console.cloud.google.com** and sign in with the same Google account.
2. Top bar → **Select a project** → **New Project**. Name: `jiotag-reviews`. Click **Create**.
3. Once created, make sure that project is selected in the top bar.
4. In the search bar at top, search for **Google Sheets API** → click it → **Enable**.
5. In the search bar, search for **Service Accounts** → click the result under "IAM & Admin".
6. Click **+ Create Service Account**. Name: `jiotag-scraper`. Click **Create and Continue** → skip the optional steps → **Done**.
7. Click the service account you just created. Go to the **Keys** tab → **Add Key** → **Create new key** → **JSON** → **Create**. A JSON file downloads. Keep it safe.
8. Open the JSON file in a text editor. You'll paste the whole contents into GitHub later.
9. Find the `client_email` in the JSON (looks like `jiotag-scraper@jiotag-reviews-xxxx.iam.gserviceaccount.com`). Copy it.
10. Go back to your Google Sheet → click **Share** → paste that email → give **Editor** access → uncheck "Notify people" → **Share**.

---

## Step 4 — Get a Gemini API key (2 min)

Google's Gemini API has a free tier that comfortably covers our use case (~50–300 reviews per week, well under the 1,000-requests-per-day free limit). No credit card needed.

1. Go to **https://aistudio.google.com/apikey**
2. Sign in with the same Google account you used for the Sheet.
3. Click **+ Create API key**.
4. If asked to select a Google Cloud project, just pick any existing one (e.g. the `jiotag-reviews` project you created in Step 3), or let Google create a new one.
5. Copy the API key it shows you — long string starting with `AIza...`. Save it.

**Note on data privacy:** on the free tier, Google may use your API inputs and outputs to improve their models. For our use case this is fine — we're only sending public review text, no internal Jio data. If this is a concern long-term, you can switch to a paid tier later without changing code.

---

## Step 5 — Add everything to GitHub secrets (5 min)

1. Go to your `jiotag-reviews` repo on GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.
2. Add one secret per row below. Name must match exactly:

| Name | Value |
|---|---|
| `GOOGLE_CREDS_JSON` | Paste the entire contents of the JSON file from Step 3.7 |
| `GOOGLE_SHEET_ID` | The Sheet ID from Step 2.3 |
| `GEMINI_API_KEY` | From Step 4.5 |

---

## Step 6 — Push the code (5 min)

Easiest way without using git on the command line:

1. Go to your empty repo on GitHub → click **uploading an existing file** link.
2. Drag and drop these files: `scrape.py`, `requirements.txt`, `README.md`, `SETUP.md`.
3. **For the workflow file**: GitHub's upload UI doesn't preserve folder structure, so do this instead:
   - Click **Add file** → **Create new file**.
   - In the filename box, type: `.github/workflows/weekly.yml` (the slashes create folders automatically).
   - Paste the contents of `weekly.yml`.
   - Scroll down → **Commit changes**.

---

## Step 7 — Test it and download your first xlsx (~25 min for first run)

1. In your repo → **Actions** tab → click **Weekly JioTag review scrape** in the left sidebar.
2. Click **Run workflow** → **Run workflow** (green button).
3. Wait. **The first run takes ~20–30 minutes** because it tags ~300 historical reviews with Gemini one at a time (free-tier rate limit). Future weekly runs will be much faster (~5 min) since only new reviews need tagging.
4. Refresh the page periodically. Click into the run to see live logs.
5. If green ✅:
   - Open your Google Sheet — should have new rows with sentiment + topic columns filled in.
   - Scroll to the bottom of the run page → **Artifacts** section → click **jiotag-reviews-xlsx** to download a zip. Unzip it. Inside is `jiotag_reviews_YYYY-MM-DD.xlsx` with three tabs: **Summary**, **New This Run**, **All Reviews**.
6. If red ❌ — click into the failed step, read the error. Most common issues:
   - Forgot to share the Sheet with the service account email (Step 3.10)
   - Typo in a secret name
   - Gemini API key invalid → regenerate at aistudio.google.com/apikey
   - YouTube returns no comments → some videos have comments disabled, this is normal; other queries should still work

---

## How to download the xlsx each week

Every Monday after the scheduled run:

1. Go to your repo → **Actions** tab.
2. Click the most recent **Weekly JioTag review scrape** run (top of the list).
3. Scroll to **Artifacts** at the bottom of the run summary page.
4. Click **jiotag-reviews-xlsx** to download.

Artifacts are kept for 90 days, so you'll always have at least the last ~12 weeks available.

**Tip:** bookmark `https://github.com/YOUR_USERNAME/jiotag-reviews/actions` so you can jump straight there.

---

## Done

From now on, the scraper runs every Monday at 09:30 IST automatically. You don't need to do anything.

Tweaks you might want later:
- Change schedule: edit the `cron` line in `weekly.yml` (use https://crontab.guru to translate)
- Add more YouTube queries: edit the top of `scrape.py`
- Add Amazon.in / Flipkart: separate task, needs paid scraper API
