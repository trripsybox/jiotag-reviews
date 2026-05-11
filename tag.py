"""
JioTag review tagger (daily run).

Finds rows in the Google Sheet that don't have sentiment tagged yet,
sends each through Gemini, and writes the results back. Tags up to
TAG_BATCH_SIZE rows per run, so a backlog of 1000+ reviews gets cleared
over a week of daily runs while staying well under the free-tier quota.

Designed to run on GitHub Actions daily.
"""

from common import (
    COLUMNS, TAG_BATCH_SIZE, TAG_DELAY_SECONDS,
    get_sheet, log, tag_with_llm,
)
import time

def main():
    log("=== JioTag tagger starting ===")
    ws = get_sheet()

    # Pull all rows. gspread returns dicts keyed by column header.
    all_rows = ws.get_all_records()
    total = len(all_rows)

    # Column indices for the cells we update (1-indexed in gspread).
    sentiment_col = COLUMNS.index("sentiment") + 1
    confidence_col = COLUMNS.index("sentiment_confidence") + 1
    topics_col = COLUMNS.index("topics") + 1
    competitor_col = COLUMNS.index("competitor_mention") + 1

    # Find rows missing sentiment.
    untagged_indices = [i for i, r in enumerate(all_rows) if not str(r.get("sentiment", "")).strip()]
    log(f"Sheet has {total} rows, {len(untagged_indices)} need tagging")

    if not untagged_indices:
        log("Nothing to tag. Exiting.")
        return

    # Cap at batch size.
    batch = untagged_indices[:TAG_BATCH_SIZE]
    log(f"Tagging {len(batch)} rows this run (cap: {TAG_BATCH_SIZE})")

    tagged = 0
    failed = 0
    for n, row_idx in enumerate(batch, 1):
        review = all_rows[row_idx]
        review_text = review.get("review_text", "")
        tags = tag_with_llm(review_text)

        if tags.get("sentiment"):
            # Sheet rows are 1-indexed, +1 again because row 1 is the header.
            sheet_row = row_idx + 2
            try:
                ws.update_cell(sheet_row, sentiment_col, tags["sentiment"])
                ws.update_cell(sheet_row, confidence_col, tags["sentiment_confidence"])
                ws.update_cell(sheet_row, topics_col, tags["topics"])
                ws.update_cell(sheet_row, competitor_col, tags["competitor_mention"])
                tagged += 1
            except Exception as e:
                log(f"  Sheet write error on row {sheet_row}: {e}")
                failed += 1
        else:
            failed += 1

        if n % 10 == 0:
            log(f"  Progress: {n}/{len(batch)} ({tagged} tagged, {failed} failed)")

        time.sleep(TAG_DELAY_SECONDS)

    log(f"=== Done: {tagged} tagged, {failed} failed, "
        f"{len(untagged_indices) - tagged} remain untagged ===")

if __name__ == "__main__":
    main()
