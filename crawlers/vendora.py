"""Vendora.gr GPU scanner. Scraped with requests + BeautifulSoup — no browser."""

import time
from datetime import datetime

from cleaning import clean_listings
from config import PAGE_DELAY, VENDORA_GPU_LOG, VENDORA_GPU_URL, VENDORA_MAX_PAGES
from crawl_utils import (_known_streak_checker, load_known_prices, log_listings,
                         new_unique)
from deals import is_real_gpu_card, match_gpu
from prices import parse_price


def _vendora_fetch_page(url: str) -> str | None:
    """Fetch a Vendora page, returning HTML string or None on failure."""
    import requests as _r
    try:
        resp = _r.get(url, timeout=30,
                      headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"),
                               "Accept-Language": "el-GR,el;q=0.9"})
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def _vendora_extract_listings(html: str) -> list[dict]:
    """Parse Vendora listing cards from HTML, returning [{name, condition, price, price_raw, url}]."""
    import bs4 as _bs4
    soup = _bs4.BeautifulSoup(html, "html.parser")
    listings = []
    for card in soup.select("a.card.vCard.card-product"):
        try:
            href = card.get("href", "")
            if not href:
                continue
            title_el = card.select_one("p.title span.body-m")
            name = title_el.get_text(strip=True) if title_el else ""
            if not name:
                continue
            price_el = card.select_one("span.label-l.tc-petrol-800")
            price_raw = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_raw)
            listings.append({"name": name, "condition": "", "price": price, "price_raw": price_raw, "url": href})
        except Exception:
            continue
    return listings


def _vendora_page_url(page: int) -> str:
    """Build a paginated Vendora URL. VENDORA_GPU_URL already carries query params
    (price_min/sort), so the page is added with & rather than ?."""
    sep = "&" if "?" in VENDORA_GPU_URL else "?"
    return f"{VENDORA_GPU_URL}{sep}page={page}"


def scan_page1_vendora_gpu(_bpage, url: str, **_kw) -> list[dict]:
    """Scan page 1 of Vendora GPU listings. _bpage is ignored (no browser needed)."""
    html = _vendora_fetch_page(url)
    if not html:
        print("  [vendora] fetch failed — skipping", flush=True)
        return []
    return _vendora_extract_listings(html)


def _initial_crawl_vendora_gpu(label: str, max_pages: int | None = None,
                               early_stop_after: int | None = None) -> dict[str, float | None]:
    """One-shot: crawl Vendora GPU listings and save to CSV. Returns the known
    URL set for the watch loop. early_stop_after stops after that many consecutive
    already-known GPU listings (see initial_crawl).

    Two hard stops keep this bounded even on the `full` tier (no early-stop):
      • VENDORA_MAX_PAGES — Vendora wraps back to page 1 past ~page 50.
      • a no-progress guard — Vendora also returns a non-empty page for out-of-range
        page numbers (it repeats the last/first page), so "empty page" never triggers.
        We stop once a page contributes zero NEW listing URLs we haven't already seen.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] ── {label.upper()}: Vendora GR initial crawl ────────────────")
    t0 = time.time()

    # Cap pages at VENDORA_MAX_PAGES regardless of the tier's max_pages.
    page_cap = VENDORA_MAX_PAGES if max_pages is None else min(max_pages, VENDORA_MAX_PAGES)

    vendora_known = load_known_prices(VENDORA_GPU_LOG)
    # Frozen snapshot: vendora_known keeps growing in the page loop below, but
    # the early-stop checker must judge against the pre-run state — dict(x) copies
    # the mapping so later writes don't retroactively count as "already known".
    hit_old = _known_streak_checker(dict(vendora_known), early_stop_after)
    seen_all: set[str] = set()   # every listing URL seen this run (any part) for the guard
    page_num = 1
    consecutive_empty = 0

    while True:
        if page_num > page_cap:
            print(f"  Stopping after {page_cap} page(s) (page cap)", flush=True)
            break
        url = _vendora_page_url(page_num)
        time.sleep(PAGE_DELAY if page_num > 1 else 0)
        html = _vendora_fetch_page(url)
        if not html:
            consecutive_empty += 1
            print(f"  Page {page_num:2}: fetch failed", flush=True)
            if consecutive_empty >= 3:
                print("  3 consecutive failures — stopping.", flush=True)
                break
            page_num += 1
            continue

        listings = _vendora_extract_listings(html)
        if not listings:
            consecutive_empty += 1
            print(f"  Page {page_num:2}: empty", flush=True)
            if consecutive_empty >= 3:
                print("  3 consecutive empty pages — stopping.", flush=True)
                break
            page_num += 1
            continue

        consecutive_empty = 0

        # No-progress guard: if this page adds no new listing URLs, Vendora has
        # wrapped/repeated — stop (covers the page-50 reset and end-of-catalog).
        page_urls = [it["url"] for it in listings]
        if not any(u not in seen_all for u in page_urls):
            print(f"  Page {page_num:2}: no new listings (Vendora repeated a page) — stopping.",
                  flush=True)
            break
        seen_all.update(page_urls)

        # Filter to GPU only. Check early-stop against the pre-run known set
        # (hit_old captured it) BEFORE vendora_known is mutated below.
        gpu_listings = [it for it in listings if match_gpu(it["name"]) is not None and is_real_gpu_card(it["name"])]
        stop_early = hit_old(gpu_listings)
        gpu_only = clean_listings(gpu_listings, "gpu")
        gpu_only = new_unique(gpu_only, vendora_known)
        if gpu_only:
            log_listings(gpu_only, ts, VENDORA_GPU_LOG)
            for it in gpu_only:
                vendora_known[it["url"]] = it.get("price")

        print(f"  Page {page_num:2}: {len(listings)} listings, "
              f"{len(gpu_only)} GPU new", flush=True)
        page_num += 1
        if stop_early:
            print(f"  Early stop: {early_stop_after} consecutive already-known listings", flush=True)
            break

    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.0f}s: {len(vendora_known)} total known URLs",
          flush=True)
    return vendora_known
