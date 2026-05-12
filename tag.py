"""
JioTag review tagger (daily run).

Finds rows in the Google Sheet that don't have sentiment tagged yet,
sends each through Gemini, and writes the results back in BATCHES (20 rows
per sheet API call) to avoid hitting Google Sheets' 60-writes-per-minute
limit. Tags up to TAG_BATCH_SIZE rows per run.

Designed to run on GitHub Actions daily.
"""

import time

from common import (
    COLUMNS, TAG_BATCH_SIZE, TAG_DELAY_SECONDS,
    get_sheet, log, tag_with_llm,
)

# How many tagged rows to accumulate before flushing to the sheet.
# Each flush = 1 batch API call (instead of 4 calls per row).
FLUSH_EVERY = 20


def flush_to_sheet(ws, pending_updates):
    """Push accumulated row updates to the sheet in a single batch_update call.

    pending_updates is a list of dicts: {row, sentiment, ...}
    """
    if not pending_updates:
        return

    sentiment_col_letter = _col_letter("sentiment")
    competitor_col_letter = _col_letter("competitor_mention")

    # Build batch payload: for each row, update the range from sentiment
    # to competitor_mention (4 contiguous columns) in one go.
    data = []
    for u in pending_updates:
        rng = f"{sentiment_col_letter}{u['row']}:{competitor_col_letter}{u['row']}"
        data.append({
            "range": rng,
            "values": [[
                u["sentiment"],
                u["sentiment_confidence"],
                u["topics"],
                u["competitor_mention"],
            ]],
        })

    try:
        ws.batch_update(data, value_input_option="RAW")
        log(f"  Flushed {len(pending_updates)} rows to sheet")
    except Exception as e:
        log(f"  Batch flush error: {e}")
        time.sleep(10)
        try:
            ws.batch_update(data, value_input_option="RAW")
            log(f"  Flushed {len(pending_updates)} rows to sheet (retry)")
        except Exception as e2:
            log(f"  Batch flush failed twice: {e2}")


def _col_letter(col_name: str) -> str:
    """Convert column name (e.g. 'sentiment') to letter (e.g. 'Q')."""
    idx = COLUMNS.index(col_name) + 1  # 1-indexed
    if idx <= 26:
        return chr(ord("A") + idx - 1)
    return chr(ord("A") + (idx - 1) // 26 - 1) + chr(ord("A") + (idx - 1) % 26)


def main():
    log("=== JioTag tagger starting ===")
    ws = get_sheet()

    all_rows = ws.get_all_records()
    total = len(all_rows)

    untagged_indices = [
        i for i, r in enumerate(all_rows)
        if not str(r.get("sentiment", "")).strip()
    ]
    log(f"Sheet has {total} rows, {len(untagged_indices)} need tagging")

    if not untagged_indices:
        log("Nothing to tag. Exiting.")
        return

    batch = untagged_indices[:TAG_BATCH_SIZE]
    log(f"Tagging {len(batch)} rows this run (cap: {TAG_BATCH_SIZE})")

    pending = []
    tagged = 0
    failed = 0

    for n, row_idx in enumerate(batch, 1):
        review = all_rows[row_idx]
        review_text = review.get("review_text", "")
        tags = tag_with_llm(review_text)

        if tags.get("sentiment"):
            pending.append({
                "row": row_idx + 2,  # +1 for 1-indexed, +1 for header
                "sentiment": tags["sentiment"],
                "sentiment_confidence": tags["sentiment_confidence"],
                "topics": tags["topics"],
                "competitor_mention": tags["competitor_mention"],
            })
            tagged += 1
        else:
            failed += 1

        # Flush periodically so partial progress is saved if we hit a timeout.
        if len(pending) >= FLUSH_EVERY:
            flush_to_sheet(ws, pending)
            pending = []

        if n % 10 == 0:
            log(f"  Progress: {n}/{len(batch)} ({tagged} tagged, {failed} failed)")

        time.sleep(TAG_DELAY_SECONDS)

    # Final flush for any leftovers.
    if pending:
        flush_to_sheet(ws, pending)

    log(f"=== Done: {tagged} tagged, {failed} failed, "
        f"{len(untagged_indices) - tagged} remain untagged ===")


if __name__ == "__main__":
    main()
