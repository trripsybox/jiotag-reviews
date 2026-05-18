"""
JioTag Dashboard builder.

Builds (or refreshes) a 'Dashboard' tab in the Google Sheet with:
  - KPI strip (totals, % negative/positive, top topic, new this week)
  - Sentiment breakdown chart
  - Top topics chart
  - Reviews by source chart
  - Weekly review volume trend chart
  - Recent negative reviews table

Reads from the 'reviews' tab. Idempotent — safe to run repeatedly.

Designed to run on GitHub Actions after the daily tagger.
"""

import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import gspread
from gspread.utils import rowcol_to_a1

from common import COLUMNS, SHEET_TAB, get_sheet, log


DASHBOARD_TAB = "Dashboard"


def parse_date(s):
    """Parse a date string from the sheet. Returns datetime or None."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.split("+")[0].split(".")[0], fmt.replace("%z", ""))
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def compute_aggregates(rows):
    """Compute all the numbers the dashboard needs from the raw rows."""
    total = len(rows)
    tagged = [r for r in rows if str(r.get("sentiment", "")).strip()]

    sentiment_counts = Counter(str(r.get("sentiment", "")).strip() for r in tagged)
    source_counts = Counter(str(r.get("source", "")).strip() for r in rows)
    competitor_count = sum(1 for r in rows if str(r.get("competitor_mention", "")).strip().lower() == "yes")

    # Topic counts (each row can have multiple comma-separated topics)
    topic_counts = Counter()
    # Topic × sentiment matrix — for the stacked bar chart.
    topic_by_sentiment = defaultdict(lambda: Counter())
    for r in tagged:
        sent = str(r.get("sentiment", "")).strip().lower()
        for t in str(r.get("topics", "")).split(","):
            t = t.strip()
            if t:
                topic_counts[t] += 1
                topic_by_sentiment[t][sent] += 1

    # Reviews per ISO week (last 12 weeks)
    weekly = defaultdict(int)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=12)
    for r in rows:
        d = parse_date(r.get("review_date", ""))
        if d and d >= cutoff:
            year, week, _ = d.isocalendar()
            weekly[f"{year}-W{week:02d}"] += 1

    # New this week
    week_ago = now - timedelta(days=7)
    new_this_week = sum(
        1 for r in rows
        if (d := parse_date(r.get("review_date", ""))) and d >= week_ago
    )

    # Recent negative reviews (latest 15)
    negatives = []
    for r in rows:
        if str(r.get("sentiment", "")).strip().lower() == "negative":
            negatives.append({
                "date": str(r.get("review_date", ""))[:10],
                "source": str(r.get("source", "")),
                "topics": str(r.get("topics", "")),
                "text": str(r.get("review_text", ""))[:200],
            })
    negatives.sort(key=lambda x: x["date"], reverse=True)
    negatives = negatives[:15]

    # Competitor mention details — collect topics and sentiments of competitor-mentioning reviews.
    competitor_reviews = []
    competitor_topic_counts = Counter()
    competitor_sentiment = Counter()
    for r in rows:
        if str(r.get("competitor_mention", "")).strip().lower() == "yes":
            text = str(r.get("review_text", ""))
            competitor_reviews.append({
                "source": str(r.get("source", "")),
                "sentiment": str(r.get("sentiment", "")),
                "topics": str(r.get("topics", "")),
                "text": text[:500],
            })
            competitor_sentiment[str(r.get("sentiment", "")).strip()] += 1
            for t in str(r.get("topics", "")).split(","):
                t = t.strip()
                if t:
                    competitor_topic_counts[t] += 1

    # Top topic
    top_topic = topic_counts.most_common(1)
    top_topic_label = top_topic[0][0] if top_topic else "—"

    # Percentages
    neg_pct = (sentiment_counts.get("negative", 0) / len(tagged) * 100) if tagged else 0
    pos_pct = (sentiment_counts.get("positive", 0) / len(tagged) * 100) if tagged else 0

    return {
        "total": total,
        "tagged": len(tagged),
        "untagged": total - len(tagged),
        "neg_pct": neg_pct,
        "pos_pct": pos_pct,
        "top_topic": top_topic_label,
        "new_this_week": new_this_week,
        "competitor_count": competitor_count,
        "sentiment_counts": sentiment_counts,
        "source_counts": source_counts,
        "topic_counts": topic_counts,
        "topic_by_sentiment": dict(topic_by_sentiment),
        "competitor_reviews": competitor_reviews,
        "competitor_topic_counts": competitor_topic_counts,
        "competitor_sentiment": competitor_sentiment,
        "weekly": dict(sorted(weekly.items())),
        "negatives": negatives,
    }


def get_or_create_dashboard_tab(sh):
    """Get the Dashboard worksheet, creating it if needed."""
    try:
        ws = sh.worksheet(DASHBOARD_TAB)
        ws.clear()  # wipe so we can re-render
        log("Cleared existing Dashboard tab")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=DASHBOARD_TAB, rows=200, cols=20)
        log("Created new Dashboard tab")
    return ws


def render_dashboard(ws, agg):
    """Write all the data + structure into the Dashboard tab in one batch."""
    # Build the entire grid of values first, then push in one update call.
    grid = [[""] * 12 for _ in range(120)]

    def put(row, col, value):
        """1-indexed cell put."""
        grid[row - 1][col - 1] = value

    # --- Header ---
    put(1, 1, "JioTag Review Dashboard")
    put(2, 1, "Last refreshed:")
    put(2, 2, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    # --- KPI strip (row 4) ---
    put(4, 1, "KEY METRICS")

    put(5, 1, "Total Reviews")
    put(6, 1, agg["total"])

    put(5, 3, "Tagged")
    put(6, 3, agg["tagged"])

    put(5, 5, "% Negative")
    put(6, 5, f"{agg['neg_pct']:.1f}%")

    put(5, 7, "% Positive")
    put(6, 7, f"{agg['pos_pct']:.1f}%")

    put(5, 9, "Top Topic")
    put(6, 9, agg["top_topic"])

    put(5, 11, "New This Week")
    put(6, 11, agg["new_this_week"])

    # --- Sentiment breakdown data (used by chart) ---
    put(9, 1, "Sentiment Breakdown")
    put(10, 1, "Sentiment")
    put(10, 2, "Count")
    sent_order = ["positive", "neutral", "negative"]
    for i, s in enumerate(sent_order):
        put(11 + i, 1, s.capitalize())
        put(11 + i, 2, agg["sentiment_counts"].get(s, 0))
    sentiment_data_end_row = 11 + len(sent_order) - 1  # row 13

    # --- Reviews by source data ---
    put(9, 4, "Reviews by Source")
    put(10, 4, "Source")
    put(10, 5, "Count")
    sorted_sources = sorted(agg["source_counts"].items(), key=lambda x: -x[1])
    for i, (src, count) in enumerate(sorted_sources):
        put(11 + i, 4, src)
        put(11 + i, 5, count)
    source_data_end_row = 11 + len(sorted_sources) - 1 if sorted_sources else 10

    # --- Top topics data ---
    put(16, 1, "Top Topics")
    put(17, 1, "Topic")
    put(17, 2, "Count")
    top_topics = agg["topic_counts"].most_common(10)
    for i, (t, c) in enumerate(top_topics):
        put(18 + i, 1, t)
        put(18 + i, 2, c)
    topics_data_end_row = 18 + len(top_topics) - 1 if top_topics else 17

    # --- Weekly volume data ---
    put(16, 4, "Weekly Review Volume (last 12 weeks)")
    put(17, 4, "Week")
    put(17, 5, "Reviews")
    weekly_items = list(agg["weekly"].items())
    for i, (week, count) in enumerate(weekly_items):
        put(18 + i, 4, week)
        put(18 + i, 5, count)
    weekly_data_end_row = 18 + len(weekly_items) - 1 if weekly_items else 17

    # --- Competitor mention summary ---
    put(31, 1, "Competitor Mentions")
    put(32, 1, "Reviews mentioning competitors:")
    put(32, 2, agg["competitor_count"])

    # --- Recent negative reviews ---
    put(35, 1, "Recent Negative Reviews")
    put(36, 1, "Date")
    put(36, 2, "Source")
    put(36, 3, "Topics")
    put(36, 4, "Review (truncated)")
    for i, neg in enumerate(agg["negatives"]):
        r = 37 + i
        put(r, 1, neg["date"])
        put(r, 2, neg["source"])
        put(r, 3, neg["topics"])
        put(r, 4, neg["text"])

    # Push the grid in one update call.
    last_row = 37 + len(agg["negatives"]) + 1
    end_a1 = rowcol_to_a1(last_row, 12)
    ws.update(range_name=f"A1:{end_a1}", values=grid[:last_row])
    log(f"Wrote {last_row} rows of data to Dashboard tab")

    return {
        "sentiment_range": ("A10", f"B{sentiment_data_end_row}"),
        "source_range": ("D10", f"E{source_data_end_row}"),
        "topics_range": ("A17", f"B{topics_data_end_row}"),
        "weekly_range": ("D17", f"E{weekly_data_end_row}"),
    }


def apply_formatting(ws):
    """Apply visual formatting: bold headers, font sizes, colors."""
    # Use a wildcard field mask — the API accepts this and applies whatever fields
    # are present in userEnteredFormat without us having to enumerate them precisely.
    fmt_mask = "userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)"

    formats = [
        # Title
        {"range": "A1", "format": {
            "textFormat": {"fontSize": 20, "bold": True,
                           "foregroundColor": {"red": 0.1, "green": 0.3, "blue": 0.7}},
        }},
        # "Last refreshed" line
        {"range": "A2:B2", "format": {
            "textFormat": {"fontSize": 10, "italic": True,
                           "foregroundColor": {"red": 0.5, "green": 0.5, "blue": 0.5}},
        }},
        # Section headers
        {"range": "A4", "format": {
            "textFormat": {"fontSize": 14, "bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.1, "green": 0.3, "blue": 0.7},
        }},
        {"range": "A9", "format": {
            "textFormat": {"fontSize": 12, "bold": True},
        }},
        {"range": "D9", "format": {
            "textFormat": {"fontSize": 12, "bold": True},
        }},
        {"range": "A16", "format": {
            "textFormat": {"fontSize": 12, "bold": True},
        }},
        {"range": "D16", "format": {
            "textFormat": {"fontSize": 12, "bold": True},
        }},
        {"range": "A31", "format": {
            "textFormat": {"fontSize": 12, "bold": True},
        }},
        {"range": "A35", "format": {
            "textFormat": {"fontSize": 12, "bold": True},
        }},
        # KPI labels (row 5)
        {"range": "A5:K5", "format": {
            "textFormat": {"fontSize": 10, "bold": True,
                           "foregroundColor": {"red": 0.4, "green": 0.4, "blue": 0.4}},
        }},
        # KPI big numbers (row 6)
        {"range": "A6:K6", "format": {
            "textFormat": {"fontSize": 22, "bold": True},
            "horizontalAlignment": "LEFT",
        }},
        # Table header rows
        {"range": "A10:E10", "format": {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
        }},
        {"range": "A17:E17", "format": {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
        }},
        {"range": "A36:D36", "format": {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
        }},
    ]

    requests = [{
        "repeatCell": {
            "range": _a1_to_grid_range(ws, f["range"]),
            "cell": {"userEnteredFormat": f["format"]},
            "fields": fmt_mask,
        }
    } for f in formats]

    # Column widths
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 160},
            "fields": "pixelSize",
        }
    })
    requests.append({
        "updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
            "properties": {"pixelSize": 400},
            "fields": "pixelSize",
        }
    })

    ws.spreadsheet.batch_update({"requests": requests})
    log("Applied formatting")


def _a1_to_grid_range(ws, a1):
    """Convert e.g. 'A4:K4' to a GridRange dict for the Sheets API."""
    if ":" not in a1:
        a1 = f"{a1}:{a1}"
    start, end = a1.split(":")

    def parse(s):
        col, row = "", ""
        for ch in s:
            if ch.isalpha():
                col += ch
            else:
                row += ch
        col_idx = 0
        for ch in col:
            col_idx = col_idx * 26 + (ord(ch.upper()) - ord("A") + 1)
        return col_idx - 1, int(row) - 1

    start_col, start_row = parse(start)
    end_col, end_row = parse(end)
    return {
        "sheetId": ws.id,
        "startRowIndex": start_row,
        "endRowIndex": end_row + 1,
        "startColumnIndex": start_col,
        "endColumnIndex": end_col + 1,
    }


def add_charts(ws, ranges):
    """Create the 4 charts on the dashboard."""
    sheet_id = ws.id

    def _source(a1_range, col_offset=0, col_width=1):
        """Build a sourceRange dict for a slice of the data range."""
        rng = _a1_to_grid_range(ws, f"{a1_range[0]}:{a1_range[1]}")
        return {"sources": [{
            "sheetId": sheet_id,
            "startRowIndex": rng["startRowIndex"],
            "endRowIndex": rng["endRowIndex"],
            "startColumnIndex": rng["startColumnIndex"] + col_offset,
            "endColumnIndex": rng["startColumnIndex"] + col_offset + col_width,
        }]}

    def _position(anchor_row, anchor_col):
        return {
            "overlayPosition": {
                "anchorCell": {
                    "sheetId": sheet_id,
                    "rowIndex": anchor_row,
                    "columnIndex": anchor_col,
                },
                "widthPixels": 480,
                "heightPixels": 300,
            }
        }

    def pie_chart(title, data_range, anchor_row, anchor_col):
        """Pie chart uses its own pieChart spec, not basicChart."""
        return {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "pieChart": {
                            "legendPosition": "RIGHT_LEGEND",
                            "domain": {"sourceRange": _source(data_range, col_offset=0, col_width=1)},
                            "series": {"sourceRange": _source(data_range, col_offset=1, col_width=1)},
                        }
                    },
                    "position": _position(anchor_row, anchor_col),
                }
            }
        }

    def basic_chart(chart_type, title, data_range, anchor_row, anchor_col):
        """Column / line / bar chart using basicChart spec."""
        return {
            "addChart": {
                "chart": {
                    "spec": {
                        "title": title,
                        "basicChart": {
                            "chartType": chart_type,
                            "legendPosition": "BOTTOM_LEGEND",
                            "axis": [
                                {"position": "BOTTOM_AXIS"},
                                {"position": "LEFT_AXIS"},
                            ],
                            "domains": [{
                                "domain": {"sourceRange": _source(data_range, col_offset=0, col_width=1)}
                            }],
                            "series": [{
                                "series": {"sourceRange": _source(data_range, col_offset=1, col_width=1)},
                                "targetAxis": "LEFT_AXIS",
                            }],
                            "headerCount": 1,
                        }
                    },
                    "position": _position(anchor_row, anchor_col),
                }
            }
        }

    requests = [
        pie_chart("Sentiment Breakdown", ranges["sentiment_range"], 8, 6),
        basic_chart("COLUMN", "Reviews by Source", ranges["source_range"], 8, 11),
        basic_chart("COLUMN", "Top Topics", ranges["topics_range"], 19, 6),
        basic_chart("LINE", "Weekly Review Volume", ranges["weekly_range"], 19, 11),
    ]

    ws.spreadsheet.batch_update({"requests": requests})
    log("Added 4 charts to dashboard")


def remove_existing_charts(ws):
    """Remove any old charts on the Dashboard tab so we can redraw fresh."""
    try:
        meta = ws.spreadsheet.fetch_sheet_metadata()
        for sheet in meta.get("sheets", []):
            if sheet["properties"]["sheetId"] != ws.id:
                continue
            charts = sheet.get("charts", [])
            if not charts:
                return
            requests = [{"deleteEmbeddedObject": {"objectId": c["chartId"]}} for c in charts]
            ws.spreadsheet.batch_update({"requests": requests})
            log(f"Removed {len(charts)} existing charts")
    except Exception as e:
        log(f"Note: couldn't remove old charts: {e}")


def main():
    log("=== Dashboard builder starting ===")
    sh = get_sheet().spreadsheet
    reviews_ws = sh.worksheet(SHEET_TAB)
    rows = reviews_ws.get_all_records()
    log(f"Loaded {len(rows)} rows from '{SHEET_TAB}' tab")

    agg = compute_aggregates(rows)
    log(f"Aggregates: total={agg['total']}, tagged={agg['tagged']}, "
        f"negative={agg['neg_pct']:.1f}%, top_topic={agg['top_topic']}")

    dash = get_or_create_dashboard_tab(sh)
    remove_existing_charts(dash)
    ranges = render_dashboard(dash, agg)
    time.sleep(1)
    apply_formatting(dash)
    time.sleep(1)
    add_charts(dash, ranges)

    log("=== Dashboard build done ===")


if __name__ == "__main__":
    main()
