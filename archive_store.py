"""
archive_store.py — keep valuable listing data alive across purges.
────────────────────────────────────────────────────────────────────────────────────────────
Sold/old listings linger in gpu_prices.csv and clutter the hunt for live deals, so the user
purges from time to time. But that data is valuable for stats (median price, future features).

So before a purge we fold the working CSV into a permanent, append-only archive (gpu_archive.csv),
**deduped by URL** (no double-adds), then clear the working CSV. The archive is stats-only — the
deal-hunting flow never reads it for "is this listing live?", only median/price history can.

Seller / negotiation history lives in negotiations.json and is NEVER touched here.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime

LIVE_CSV = "gpu_prices.csv"
ARCHIVE_CSV = "gpu_archive.csv"
FIELDS = ["timestamp", "name", "condition", "price", "url"]
SOLD_FIELDS = ["timestamp", "name", "condition", "price", "url", "detected_via"]


def _read_rows(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _archive_urls(path: str = ARCHIVE_CSV) -> set[str]:
    return {(r.get("url") or "").strip() for r in _read_rows(path) if (r.get("url") or "").strip()}


def archive_count(path: str = ARCHIVE_CSV) -> int:
    return len(_read_rows(path))


def live_count(path: str = LIVE_CSV) -> int:
    return len(_read_rows(path))


def fold_into_archive(live_path: str = LIVE_CSV, archive_path: str = ARCHIVE_CSV) -> int:
    """Append live rows whose URL is not already archived. Returns rows added. No clearing.

    Live CSVs can now have multiple rows per URL (one per observed price change,
    see crawl_utils.load_known_prices). Keep the LAST row for each URL — the
    freshest price — so a purge preserves the most accurate price history."""
    live = _read_rows(live_path)
    if not live:
        return 0
    have = _archive_urls(archive_path)
    latest: dict[str, dict] = {}
    for r in live:
        url = (r.get("url") or "").strip()
        if url and url not in have:
            latest[url] = {k: r.get(k, "") for k in FIELDS}   # later rows overwrite earlier
    if not latest:
        return 0
    new_rows = list(latest.values())
    exists = os.path.isfile(archive_path)
    with open(archive_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(new_rows)
    return len(new_rows)


def clear_live(live_path: str = LIVE_CSV) -> int:
    """Truncate the working CSV to just its header. Returns rows removed."""
    rows = _read_rows(live_path)
    with open(live_path, "w", newline="", encoding="utf-8") as fh:
        csv.DictWriter(fh, fieldnames=FIELDS).writeheader()
    return len(rows)


def archive_and_purge(live_path: str = LIVE_CSV, archive_path: str = ARCHIVE_CSV) -> dict:
    """Archive-before-purge: fold live → archive (dedup by URL), then clear the working CSV."""
    before_live = live_count(live_path)
    added = fold_into_archive(live_path, archive_path)
    cleared = clear_live(live_path)
    return {
        "live_before": before_live,
        "archived_new": added,
        "cleared": cleared,
        "archive_total": archive_count(archive_path),
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def sold_path_for(log_file: str) -> str:
    """gpu_prices.csv → gpu_sold.csv. Any *.csv → *_sold.csv. Idempotent."""
    if log_file.endswith("_sold.csv"):
        return log_file
    return re.sub(r"(_prices)?\.csv$", "_sold.csv", log_file)


def record_sold_tagged(rows: list[dict], log_file: str, detected_via: str) -> int:
    """Tag rows with `detected_via` and archive to log_file's sold sidecar. Swallows
    exceptions — a failing sold-archive must not break the calling crawl/verify pass.
    Returns rows written (0 on error, empty input, or missing log_file)."""
    if not rows or not log_file:
        return 0
    try:
        tagged = [{**r, "detected_via": detected_via} for r in rows]
        return record_sold(tagged, sold_path_for(log_file))
    except Exception as e:
        print(f"  [sold-archive] skipped: {str(e)[:80]}", flush=True)
        return 0


def record_sold(rows: list[dict], sold_path: str) -> int:
    """Append sold rows to the per-part sold archive, deduped by URL. Header if new file.
    Caller supplies `detected_via` on each row; timestamp is filled in from now() if missing.
    Within a batch, later rows overwrite earlier for the same URL (matches
    `fold_into_archive`'s last-wins semantics, so a sold URL's latest price is what lands
    even when the live CSV holds a price-history trail for it). Rows whose URL is already
    in the sold archive are skipped (first-sale-observation wins across calls).
    Returns rows actually written."""
    if not rows:
        return 0
    have = _archive_urls(sold_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fresh: dict[str, dict] = {}
    for r in rows:
        url = (r.get("url") or "").strip()
        if not url or url in have:
            continue
        row = {}
        for k in SOLD_FIELDS:
            v = r.get(k)
            row[k] = v if v not in (None, "") else (now if k == "timestamp" else "")
        fresh[url] = row   # later rows overwrite earlier — freshest price wins
    if not fresh:
        return 0
    exists = os.path.isfile(sold_path)
    with open(sold_path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SOLD_FIELDS)
        if not exists:
            w.writeheader()
        w.writerows(fresh.values())
    return len(fresh)


def read_for_stats(live_path: str = LIVE_CSV, archive_path: str = ARCHIVE_CSV) -> list[dict]:
    """Live + archive, deduped by URL (live row wins). For median/price-history only."""
    out: dict[str, dict] = {}
    for r in _read_rows(archive_path):
        url = (r.get("url") or "").strip()
        if url:
            out[url] = r
    for r in _read_rows(live_path):   # live overrides archive (fresher price)
        url = (r.get("url") or "").strip()
        if url:
            out[url] = r
    return list(out.values())


if __name__ == "__main__":
    import json
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"live={live_count()} archive={archive_count()}")
    if len(sys.argv) > 1 and sys.argv[1] == "purge":
        print(json.dumps(archive_and_purge(), indent=2))
