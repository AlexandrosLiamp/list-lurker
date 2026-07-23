"""Skroutz Skoop (classifieds) scraping — the primary source.

Pagination uses Turbo Drive's `a.next` and requires waiting for the sku-card
grid to re-render (js_navigate_next). Extraction is card-scoped and drops
sold badges as it goes; is_clean / clean_listings run afterwards to filter
wanted-ads / trades / bundles."""

import csv
import os
import re
import time
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from listing_common import _norm
from config import NAV_TIMEOUT, PAGE_DELAY
from prices import parse_price
from cleaning import is_broken, clean_listings
from crawl_utils import new_unique, _known_streak_checker, log_listings
from archive_store import record_sold_tagged


def wait_for_cards(page, timeout: int = 15000) -> None:
    try:
        page.wait_for_selector("li.sku-card", timeout=timeout)
    except PlaywrightTimeout:
        pass


def get_card_hrefs(page) -> set[str]:
    return set(page.evaluate(
        "() => Array.from(document.querySelectorAll('li.sku-card a.link'))"
        ".map(a => a.getAttribute('href'))"
    ))


def get_total_pages(page) -> int:
    try:
        el = page.query_selector(".paginator-button span")
        if el:
            m = re.search(r"από\s+(\d+)", el.inner_text())
            if m: return int(m.group(1))
    except Exception:
        pass
    return 1


def js_navigate_next(page, prev_hrefs: set[str]) -> bool:
    href = page.evaluate(
        "() => { const a = document.querySelector('a.next');"
        " return a ? a.getAttribute('href') : null; }"
    )
    if not href:
        return False
    page.evaluate(
        f"() => {{ if (window.Turbo) {{ Turbo.visit('{href}'); }}"
        f" else {{ window.location.href = '{href}'; }} }}"
    )
    deadline = time.time() + NAV_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            new_hrefs = get_card_hrefs(page)
            if new_hrefs and new_hrefs != prev_hrefs:
                return True
        except Exception:
            pass
    return False


def extract_listings(page) -> tuple[list[dict], list[dict]]:
    """Returns (live_listings, sold_listings). Sold cards are captured for archival
    rather than dropped — see `sold-price-archive-plan` decision."""
    listings, sold_listings = [], []
    for card in page.query_selector_all("li.sku-card"):
        try:
            name_el  = card.query_selector("h2.sku-card-title")
            name     = name_el.inner_text().strip() if name_el else ""
            if not name: continue
            if is_broken(name): continue

            price_el  = card.query_selector("span.item-price")
            price_raw = price_el.inner_text().strip() if price_el else ""
            price     = parse_price(price_raw)

            cond_el   = card.query_selector("span.condition")
            condition = cond_el.inner_text().strip() if cond_el else ""

            link_el = card.query_selector("a.link")
            href    = (link_el.get_attribute("href") or "") if link_el else ""
            url     = ("https://www.skroutz.gr" + href) if href.startswith("/") else href

            item = {"name": name, "condition": condition,
                    "price": price, "price_raw": price_raw, "url": url}

            # Sold cards go to the sold sink, not live listings — check badge/overlay and
            # the full card text (accent-safe).
            try:
                if "πωληθηκε" in _norm(card.inner_text()):
                    sold_listings.append(item)
                    continue
            except Exception:
                pass

            listings.append(item)
        except Exception:
            continue
    return listings, sold_listings


def scan_page1_skroutz(bpage, url: str, log_file: str | None = None, **_kw) -> list[dict]:
    try:
        bpage.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        print("  [scan] Reload timed out — skipping", flush=True)
        return []
    wait_for_cards(bpage, timeout=10000)
    listings, sold = extract_listings(bpage)
    record_sold_tagged(sold, log_file, "badge_feed")
    return listings


def initial_crawl(bpage, already_known: dict[str, float | None], base_url: str,
                  log_file: str, label: str,
                  log_filter=None, kind: str | None = None,
                  max_pages: int | None = None,
                  early_stop_after: int | None = None) -> dict[str, float | None]:
    """
    Crawl pages of base_url, logging listings not already in already_known.
      max_pages        — hard cap on pages crawled (None = until the last page).
      early_stop_after — stop once this many *consecutive* already-known listings
                         are seen (feeds are newest-first, so a run of known
                         listings means we've reached old ground). None = off.
    Returns the updated known-URL set.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] ── {label} INITIAL CRAWL ──────────────────────────────")
    t0 = time.time()
    hit_old = _known_streak_checker(already_known, early_stop_after)

    bpage.goto(base_url, wait_until="domcontentloaded", timeout=60000)
    wait_for_cards(bpage)
    total_pages = get_total_pages(bpage)

    all_listings: list[dict] = []
    all_sold: list[dict] = []
    page1, sold1 = extract_listings(bpage)
    all_listings.extend(page1)
    all_sold.extend(sold1)
    print(f"  Page  1/{total_pages}: {len(page1)} listings", flush=True)
    prev_hrefs = get_card_hrefs(bpage)
    stop_early = hit_old(page1)

    for n in range(2, total_pages + 1):
        if stop_early:
            print(f"  Early stop: {early_stop_after} consecutive already-known listings", flush=True)
            break
        if max_pages is not None and n > max_pages:
            print(f"  Stopping after {max_pages} page(s) (max_pages limit)", flush=True)
            break
        time.sleep(PAGE_DELAY)
        if not js_navigate_next(bpage, prev_hrefs):
            print(f"  Page {n:2}/{total_pages}: nav failed — stopping", flush=True)
            break
        listings, sold = extract_listings(bpage)
        if not listings and not sold:
            print(f"  Page {n:2}/{total_pages}: empty — stopping", flush=True)
            break
        all_listings.extend(listings)
        all_sold.extend(sold)
        print(f"  Page {n:2}/{total_pages}: {len(listings)} listings", flush=True)
        prev_hrefs = get_card_hrefs(bpage)
        stop_early = hit_old(listings)

    record_sold_tagged(all_sold, log_file, "badge_feed")

    elapsed = time.time() - t0

    to_log = all_listings
    if kind:
        to_log = clean_listings(to_log, kind)
    if log_filter:
        to_log = [item for item in to_log if log_filter(item)]

    new_listings = new_unique(to_log, already_known)
    if new_listings:
        log_listings(new_listings, ts, log_file)

    known = {**already_known, **{item["url"]: item.get("price") for item in to_log if item["url"]}}
    skipped = len(to_log) - len(new_listings)
    filtered_out = len(all_listings) - len(to_log)

    print(f"\n  Done in {elapsed:.0f}s: {len(all_listings)} seen"
          + (f", {filtered_out} filtered/cleaned out" if filtered_out else "")
          + f", {len(new_listings)} new logged, {skipped} already in CSV")
    return known


# ── Sold-listing verification (skoop product pages only) ──────────────────────

def is_sold(page) -> bool:
    """True if a Skroutz skoop product page shows the disabled 'Πωλήθηκε' button."""
    try:
        for b in page.query_selector_all("button"):
            try:
                if "πωληθηκε" in _norm(b.inner_text()):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def verify_sold(bpage, log_files: list[str]) -> None:
    """Visit each unique skoop listing's product page and remove sold ones from
    the CSVs. Only skoop URLs are checked (insomnia/retail are skipped)."""
    t0 = time.time()
    rows_by_file: dict[str, list[dict]] = {}
    urls: set[str] = set()
    for lf in log_files:
        if not os.path.isfile(lf):
            continue
        with open(lf, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        rows_by_file[lf] = rows
        for r in rows:
            u = (r.get("url") or "").strip()
            if "/skoop/" in u:
                urls.add(u)

    urls = sorted(urls)
    print(f"\n── SOLD VERIFICATION ── {len(urls)} unique skoop listings to check")
    sold: set[str] = set()
    for i, u in enumerate(urls, 1):
        try:
            bpage.goto(u, wait_until="domcontentloaded", timeout=30000)
            time.sleep(0.5)
            if is_sold(bpage):
                sold.add(u)
        except Exception as e:
            print(f"  [{i}/{len(urls)}] error: {str(e)[:60]}", flush=True)
        if i % 25 == 0:
            print(f"  …{i}/{len(urls)} checked, {len(sold)} sold so far", flush=True)

    total_removed = 0
    for lf, rows in rows_by_file.items():
        removed_rows = [r for r in rows if (r.get("url") or "").strip() in sold]
        kept = [r for r in rows if (r.get("url") or "").strip() not in sold]
        removed = len(rows) - len(kept)
        if removed:
            record_sold_tagged(removed_rows, lf, "badge_page")
            with open(lf, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp", "name", "condition", "price", "url"])
                w.writeheader()
                w.writerows(kept)
            print(f"  {lf}: removed {removed} sold rows")
            total_removed += removed

    print(f"Done in {time.time()-t0:.0f}s: {len(sold)} sold listings, {total_removed} CSV rows pruned.")
