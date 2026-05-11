"""
JioTag review scraper (weekly run).

Scrapes Google Play, Apple App Store, and YouTube. Dedupes against the Google
Sheet. Appends new reviews to the sheet with sentiment/topics LEFT BLANK —
tagging happens separately in tag.py running daily.

This script is fast (~5 min) because it does no LLM calls. Designed to run on
GitHub Actions weekly.
"""

import os
import time
from datetime import datetime, timezone

import requests

from common import (
    APP_STORE_APP_ID, APP_STORE_COUNTRY, COLUMNS, PLAY_STORE_APP_ID,
    YOUTUBE_MAX_COMMENTS_PER_VIDEO, YOUTUBE_MAX_VIDEOS_PER_QUERY, YOUTUBE_QUERIES,
    append_reviews, existing_ids, fetch_all_rows, get_sheet, log, make_id, now_iso,
)

# --- Scrapers ----------------------------------------------------------------

def scrape_play_store():
    from google_play_scraper import Sort, reviews
    log("Scraping Play Store...")
    out = []
    try:
        result, _ = reviews(
            PLAY_STORE_APP_ID,
            lang="en",
            country="in",
            sort=Sort.NEWEST,
            count=200,
        )
        for r in result:
            out.append({
                "review_id": make_id("play_store", r["reviewId"]),
                "source": "play_store",
                "product_variant": "JioThings app",
                "product_url": f"https://play.google.com/store/apps/details?id={PLAY_STORE_APP_ID}",
                "review_date": r["at"].isoformat() if r.get("at") else "",
                "scrape_date": now_iso(),
                "rating": r.get("score", ""),
                "review_title": "",
                "review_text": r.get("content", "") or "",
                "reviewer_name": r.get("userName", "") or "",
                "verified_purchase": "",
                "helpful_votes": r.get("thumbsUpCount", 0),
                "app_version": r.get("reviewCreatedVersion", "") or "",
                "device_info": "",
                "language": "en",
                "media_attached": "",
                "response_from_seller": "yes" if r.get("replyContent") else "no",
            })
        log(f"  Play Store: {len(out)} reviews fetched")
    except Exception as e:
        log(f"  Play Store error: {e}")
    return out

def scrape_app_store():
    log("Scraping App Store...")
    out = []
    try:
        for page in range(1, 6):
            url = (f"https://itunes.apple.com/{APP_STORE_COUNTRY}/rss/customerreviews/"
                   f"page={page}/id={APP_STORE_APP_ID}/sortby=mostrecent/json")
            resp = requests.get(url, timeout=20)
            if resp.status_code != 200:
                break
            data = resp.json()
            entries = data.get("feed", {}).get("entry", [])
            if page == 1 and entries and "im:name" in entries[0]:
                entries = entries[1:]
            if not entries:
                break
            for e in entries:
                rid = e.get("id", {}).get("label", "")
                if not rid:
                    continue
                out.append({
                    "review_id": make_id("app_store", rid),
                    "source": "app_store",
                    "product_variant": "JioThings app",
                    "product_url": f"https://apps.apple.com/{APP_STORE_COUNTRY}/app/id{APP_STORE_APP_ID}",
                    "review_date": e.get("updated", {}).get("label", ""),
                    "scrape_date": now_iso(),
                    "rating": e.get("im:rating", {}).get("label", ""),
                    "review_title": e.get("title", {}).get("label", ""),
                    "review_text": e.get("content", {}).get("label", ""),
                    "reviewer_name": e.get("author", {}).get("name", {}).get("label", ""),
                    "verified_purchase": "",
                    "helpful_votes": e.get("im:voteSum", {}).get("label", 0),
                    "app_version": e.get("im:version", {}).get("label", ""),
                    "device_info": "iOS",
                    "language": "en",
                    "media_attached": "",
                    "response_from_seller": "",
                })
            time.sleep(0.5)
        log(f"  App Store: {len(out)} reviews fetched")
    except Exception as e:
        log(f"  App Store error: {e}")
    return out

def scrape_youtube():
    from youtube_comment_downloader import YoutubeCommentDownloader
    import yt_dlp
    log("Scraping YouTube...")
    out = []
    try:
        downloader = YoutubeCommentDownloader()
        ydl_opts = {"quiet": True, "extract_flat": True, "skip_download": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for query in YOUTUBE_QUERIES:
                search_url = f"ytsearch{YOUTUBE_MAX_VIDEOS_PER_QUERY}:{query}"
                info = ydl.extract_info(search_url, download=False)
                for entry in info.get("entries", []):
                    video_id = entry.get("id")
                    video_title = entry.get("title", "")
                    if not video_id:
                        continue
                    video_url = f"https://www.youtube.com/watch?v={video_id}"
                    count = 0
                    for c in downloader.get_comments_from_url(video_url, sort_by=0):
                        if count >= YOUTUBE_MAX_COMMENTS_PER_VIDEO:
                            break
                        count += 1
                        out.append({
                            "review_id": make_id("youtube", f"{video_id}:{c.get('cid', '')}"),
                            "source": "youtube",
                            "product_variant": "JioTag (general)",
                            "product_url": video_url,
                            "review_date": c.get("time", ""),
                            "scrape_date": now_iso(),
                            "rating": "",
                            "review_title": f"Comment on: {video_title}",
                            "review_text": c.get("text", "") or "",
                            "reviewer_name": c.get("author", "") or "",
                            "verified_purchase": "",
                            "helpful_votes": c.get("votes", 0),
                            "app_version": "",
                            "device_info": "",
                            "language": "en",
                            "media_attached": "no",
                            "response_from_seller": "",
                        })
                    time.sleep(0.5)
        log(f"  YouTube: {len(out)} comments fetched")
    except Exception as e:
        log(f"  YouTube error: {e}")
    return out

# --- xlsx export -------------------------------------------------------------

def write_xlsx(new_rows, all_rows, path: str):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"

    n = len(new_rows)
    by_source = {}
    by_sentiment = {"positive": 0, "neutral": 0, "negative": 0, "": 0}
    topic_counts = {}
    untagged = 0
    negative_examples = []
    for r in new_rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        s = r.get("sentiment", "") or ""
        by_sentiment[s] = by_sentiment.get(s, 0) + 1
        if not s:
            untagged += 1
        for t in (r.get("topics", "") or "").split(","):
            t = t.strip()
            if t:
                topic_counts[t] = topic_counts.get(t, 0) + 1
        if r.get("sentiment") == "negative" and len(negative_examples) < 10:
            snippet = (r.get("review_text") or "").strip().replace("\n", " ")
            if len(snippet) > 250:
                snippet = snippet[:250] + "..."
            negative_examples.append({
                "source": r["source"],
                "topics": r.get("topics", ""),
                "text": snippet,
            })

    bold = Font(bold=True, size=12)
    title_font = Font(bold=True, size=16)
    header_fill = PatternFill("solid", fgColor="DDDDDD")

    ws["A1"] = "JioTag Review Scrape"
    ws["A1"].font = title_font
    ws["A2"] = f"Run date: {now_iso()}"
    ws["A3"] = f"New reviews this run: {n}"
    ws["A3"].font = bold

    if untagged > 0:
        ws["A4"] = (f"Note: {untagged} reviews not yet tagged. Sentiment/topics "
                    f"will be filled in by the daily tagger over the next few days.")
        ws["A4"].font = Font(italic=True, size=10, color="888888")

    row = 6
    ws.cell(row=row, column=1, value="By source").font = bold
    row += 1
    for k, v in by_source.items():
        ws.cell(row=row, column=1, value=k)
        ws.cell(row=row, column=2, value=v)
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="Sentiment (of tagged reviews)").font = bold
    row += 1
    for label, key in [("Positive", "positive"), ("Neutral", "neutral"), ("Negative", "negative"), ("Not yet tagged", "")]:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=by_sentiment.get(key, 0))
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="Top topics").font = bold
    row += 1
    for t, c in sorted(topic_counts.items(), key=lambda x: -x[1])[:10]:
        ws.cell(row=row, column=1, value=t)
        ws.cell(row=row, column=2, value=c)
        row += 1

    if negative_examples:
        row += 1
        ws.cell(row=row, column=1, value="Negative review snippets").font = bold
        row += 1
        ws.cell(row=row, column=1, value="Source").font = bold
        ws.cell(row=row, column=2, value="Topics").font = bold
        ws.cell(row=row, column=3, value="Review").font = bold
        row += 1
        for ex in negative_examples:
            ws.cell(row=row, column=1, value=ex["source"])
            ws.cell(row=row, column=2, value=ex["topics"])
            ws.cell(row=row, column=3, value=ex["text"]).alignment = Alignment(wrap_text=True)
            row += 1

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 30
    ws.column_dimensions["C"].width = 80

    ws2 = wb.create_sheet("New This Run")
    ws2.append(COLUMNS)
    for cell in ws2[1]:
        cell.font = bold
        cell.fill = header_fill
    for r in new_rows:
        ws2.append([r.get(c, "") for c in COLUMNS])
    _autosize(ws2)

    ws3 = wb.create_sheet("All Reviews")
    ws3.append(COLUMNS)
    for cell in ws3[1]:
        cell.font = bold
        cell.fill = header_fill
    for r in all_rows:
        ws3.append([r.get(c, "") for c in COLUMNS])
    _autosize(ws3)

    wb.save(path)
    log(f"xlsx written: {path}")

def _autosize(ws):
    from openpyxl.utils import get_column_letter
    widths = {
        "review_id": 18, "source": 12, "product_variant": 18,
        "product_url": 30, "review_date": 22, "scrape_date": 22,
        "rating": 8, "review_title": 30, "review_text": 60,
        "reviewer_name": 18, "verified_purchase": 10, "helpful_votes": 10,
        "app_version": 12, "device_info": 12, "language": 10,
        "media_attached": 10, "sentiment": 12, "sentiment_confidence": 10,
        "topics": 30, "competitor_mention": 10, "response_from_seller": 12,
    }
    for i, col_name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(col_name, 15)

# --- Main --------------------------------------------------------------------

def main():
    log("=== JioTag review scrape starting ===")
    ws = get_sheet()
    seen = existing_ids(ws)
    log(f"Sheet has {len(seen)} existing reviews")

    all_scraped = []
    all_scraped += scrape_play_store()
    all_scraped += scrape_app_store()
    all_scraped += scrape_youtube()

    new_rows = [r for r in all_scraped if r["review_id"] not in seen]
    log(f"Total scraped: {len(all_scraped)} | New: {len(new_rows)}")

    # Leave tagging columns blank — tag.py fills them in over the week.
    for r in new_rows:
        r.setdefault("sentiment", "")
        r.setdefault("sentiment_confidence", "")
        r.setdefault("topics", "")
        r.setdefault("competitor_mention", "")

    append_reviews(ws, new_rows)
    log(f"Appended {len(new_rows)} rows to sheet")

    all_rows = fetch_all_rows(ws)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    xlsx_path = f"jiotag_reviews_{date_str}.xlsx"
    write_xlsx(new_rows, all_rows, xlsx_path)
    log("=== Done ===")

if __name__ == "__main__":
    main()
