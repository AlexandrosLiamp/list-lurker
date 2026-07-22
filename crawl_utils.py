"""Shared helpers used across every crawler / retail / watch module.

Grouping them here (instead of leaving them in monitor.py) is what lets the
per-source crawler modules import from a common place without pulling in
monitor's CLI + main() — that transitive weight caused the pre-refactor
circular-import risk."""

import csv
import ctypes
import os
import threading

from prices import csv_price


# ── Cross-thread page-hang guard ──────────────────────────────────────────────
# Playwright's sync page.evaluate() has no timeout; a wedged page blocks the
# calling thread forever. page_timeout starts a background timer and, if it
# fires, injects a PageTimeoutException into the target thread so the caller's
# try/except can catch what would otherwise be an uncatchable hang.
class PageTimeoutException(Exception):
    """Exception raised when a page navigation or extraction operation hangs."""
    pass


class page_timeout:
    """Context manager to raise PageTimeoutException in the main thread if it blocks too long."""
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.thread_id = threading.get_ident()
        self.timer = None

    def __enter__(self):
        if self.seconds > 0:
            self.timer = threading.Timer(self.seconds, self._trigger)
            self.timer.daemon = True
            self.timer.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.timer:
            self.timer.cancel()
        return False

    def _trigger(self):
        target_tid = ctypes.c_long(self.thread_id)
        ctypes.pythonapi.PyThreadState_SetAsyncExc(target_tid, ctypes.py_object(PageTimeoutException))


# ── CSV / URL bookkeeping ─────────────────────────────────────────────────────

def load_known_prices(log_file: str) -> dict[str, float | None]:
    """Return {url: latest_logged_price} — later CSV rows overwrite earlier ones,
    so a URL that appears multiple times (once we start recording price changes)
    resolves to its most recent price. Uses csv_price to avoid the parse_price
    10x-inflation trap on canonical-float rows (see bugs/parse-price-csv-inflation)."""
    if not os.path.isfile(log_file):
        return {}
    known: dict[str, float | None] = {}
    with open(log_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # `or ""` guards against malformed/short rows where DictReader yields
            # None for a missing trailing field (otherwise .strip() crashes).
            url = (row.get("url") or "").strip()
            if url:
                known[url] = csv_price(row.get("price"))
    print(f"  {log_file}: {len(known)} existing URLs loaded")
    return known


def price_changed(old: float | None, new: float | None) -> bool:
    """True iff both prices are known and differ (rounded to cents). A blank/
    unparseable price on either side is never a change — avoids re-logging on
    transient scrape flakiness."""
    return old is not None and new is not None and round(old, 2) != round(new, 2)


def new_unique(items: list[dict], known: dict[str, float | None]) -> list[dict]:
    """Items that are either not-yet-known, or known-but-with-a-changed-price.
    Also drops within-batch duplicates (same listing on overlapping pages)."""
    out, seen = [], set()
    for it in items:
        u = (it.get("url") or "").strip()
        if not u or u in seen:
            continue
        if u not in known or price_changed(known.get(u), it.get("price")):
            seen.add(u)
            out.append(it)
    return out


def _known_streak_checker(known: dict[str, float | None], threshold: int | None):
    """Build the 'crawl' tier's early-stop checker.

    Returns f(listings) -> bool. Across successive pages it tracks the running
    count of *consecutive* listings whose URL is already in `known`. The feeds
    are newest-first, so a long run of known listings means we've reached ground
    we already have — f() returns True once the streak reaches `threshold`. A
    brand-new listing resets the streak. Always False when threshold is None
    (full crawls never early-stop)."""
    streak = 0
    def check(listings) -> bool:
        nonlocal streak
        if threshold is None:
            return False
        for it in listings:
            u = (it.get("url") or "").strip()
            if u and u in known:
                streak += 1
                if streak >= threshold:
                    return True
            else:
                streak = 0
        return False
    return check


def log_listings(listings: list[dict], timestamp: str, log_file: str) -> None:
    file_exists = os.path.isfile(log_file)
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "name", "condition", "price", "url"])
        if not file_exists:
            writer.writeheader()
        for item in listings:
            writer.writerow({"timestamp": timestamp, "name": item["name"],
                             "condition": item["condition"], "price": item["price"],
                             "url": item["url"]})


def prune_urls(log_file: str, urls: set[str]) -> int:
    """Rewrite log_file with rows whose URL isn't in `urls`. Returns how many
    rows were removed. Used by AI-verify (drop sold listings) and by the wanted-ad
    purge (drop Ζήτηση ads pulled from insomnia). No-op if the file is missing
    or `urls` is empty."""
    if not os.path.isfile(log_file) or not urls:
        return 0
    with open(log_file, encoding="utf-8") as f:
        rdr = csv.DictReader(f); fields = rdr.fieldnames; rows = list(rdr)
    kept = [r for r in rows if (r.get("url") or "").strip() not in urls]
    removed = len(rows) - len(kept)
    if removed:
        with open(log_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(kept)
    return removed


def recreate_page(ctx):
    """Create a fresh page in the existing browser context. Used by every
    long-running crawl for crash recovery when the current page dies."""
    try:
        page = ctx.new_page()
        print("  [recovery] New browser page created.", flush=True)
        return page
    except Exception as e:
        print(f"  [recovery] Failed to create new page: {e}", flush=True)
        return None
