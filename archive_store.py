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
from datetime import datetime

LIVE_CSV = "gpu_prices.csv"
ARCHIVE_CSV = "gpu_archive.csv"
FIELDS = ["timestamp", "name", "condition", "price", "url"]


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
    """Append live rows whose URL is not already archived. Returns rows added. No clearing."""
    live = _read_rows(live_path)
    if not live:
        return 0
    have = _archive_urls(archive_path)
    new_rows = []
    seen_now: set[str] = set()
    for r in live:
        url = (r.get("url") or "").strip()
        if not url or url in have or url in seen_now:
            continue
        seen_now.add(url)
        new_rows.append({k: r.get(k, "") for k in FIELDS})
    if not new_rows:
        return 0
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
