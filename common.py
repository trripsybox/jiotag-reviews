"""Shared helpers used by both scrape.py (weekly) and tag.py (daily)."""

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

# --- Configuration -----------------------------------------------------------

PLAY_STORE_APP_ID = "com.jio.consumer.jiothings"   # JioThings on Play Store
APP_STORE_APP_ID = "1549371816"                     # JioThings on App Store
APP_STORE_COUNTRY = "in"

YOUTUBE_QUERIES = ["JioTag review", "JioTag Air review", "JioTag Go review"]
YOUTUBE_MAX_VIDEOS_PER_QUERY = 5
YOUTUBE_MAX_COMMENTS_PER_VIDEO = 100

SHEET_TAB = "reviews"

COLUMNS = [
    "review_id", "source", "product_variant", "product_url",
    "review_date", "scrape_date", "rating", "review_title", "review_text",
    "reviewer_name", "verified_purchase", "helpful_votes",
    "app_version", "device_info", "language", "media_attached",
    "sentiment", "sentiment_confidence", "topics", "competitor_mention",
    "response_from_seller",
]

TOPIC_TAGS = [
    "battery", "accuracy", "range", "app_connectivity", "find_my_device",
    "price", "build_quality", "customer_service", "setup", "community_find",
    "compatibility", "other",
]

# Tag.py settings — tuned for Gemini free tier (15 req/min, 1000 req/day)
TAG_BATCH_SIZE = 100              # max rows to tag per daily run (~10 min total)
TAG_DELAY_SECONDS = 5             # 12 req/min, safely under 15/min limit
TAG_MIN_TEXT_LENGTH = 15          # skip Gemini for short noise like "Nope" / "Hi sir"

# --- Helpers -----------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def make_id(source: str, native_id: str) -> str:
    """Stable unique ID per review across runs, used for dedup."""
    h = hashlib.sha1(f"{source}:{native_id}".encode()).hexdigest()[:16]
    return f"{source}_{h}"

def log(msg: str):
    print(f"[{now_iso()}] {msg}", flush=True)

# --- Google Sheets I/O -------------------------------------------------------

def get_sheet():
    """Authorize and return the reviews worksheet."""
    creds_json = os.environ["GOOGLE_CREDS_JSON"]
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=1, cols=len(COLUMNS))
        ws.append_row(COLUMNS)
    header = ws.row_values(1)
    if header != COLUMNS:
        ws.update("A1", [COLUMNS])
    return ws

def existing_ids(ws):
    """Return set of review_ids already in the sheet."""
    col = ws.col_values(1)
    return set(col[1:])

def append_reviews(ws, rows):
    if not rows:
        return
    values = [[r.get(c, "") for c in COLUMNS] for r in rows]
    ws.append_rows(values, value_input_option="RAW")

def fetch_all_rows(ws):
    """Return all rows from the sheet as list of dicts."""
    return ws.get_all_records()

# --- Gemini tagging ----------------------------------------------------------

def tag_with_llm(review_text: str) -> dict:
    """Send a review to Gemini and get back sentiment + topic tags.
    Retries on 429/503 with exponential backoff."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _empty_tag()

    # Short noise (e.g. "Nope", "Hi") — skip the API call to save quota.
    if not review_text or len(review_text.strip()) < TAG_MIN_TEXT_LENGTH:
        return {
            "sentiment": "neutral",
            "sentiment_confidence": 0.3,
            "topics": "other",
            "competitor_mention": "no",
        }

    prompt = f"""Classify this product review of JioTag (a Bluetooth item tracker similar to Apple AirTag, made by Reliance Jio in India).

Review: \"\"\"{review_text[:2000]}\"\"\"

Respond with ONLY a JSON object, no prose, no markdown:
{{
  "sentiment": "positive" | "neutral" | "negative",
  "sentiment_confidence": 0.0-1.0,
  "topics": [list of applicable tags from: {TOPIC_TAGS}],
  "competitor_mention": true | false (true if AirTag, Tile, SmartTag, or other competitor is named)
}}"""

    model = "gemini-2.5-flash-lite"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 300,
            "responseMimeType": "application/json",
        },
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    backoff = 8
    for attempt in range(4):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code in (429, 503):
                log(f"  Gemini {resp.status_code} on attempt {attempt + 1}, backing off {backoff}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            parsed = json.loads(text.strip())
            return {
                "sentiment": parsed.get("sentiment", ""),
                "sentiment_confidence": parsed.get("sentiment_confidence", ""),
                "topics": ",".join(parsed.get("topics", [])),
                "competitor_mention": "yes" if parsed.get("competitor_mention") else "no",
            }
        except Exception as e:
            log(f"  Gemini tagging error (attempt {attempt + 1}): {e}")
            if attempt < 3:
                time.sleep(backoff)
                backoff *= 2
            continue

    return _empty_tag()

def _empty_tag():
    return {"sentiment": "", "sentiment_confidence": "",
            "topics": "", "competitor_mention": ""}
