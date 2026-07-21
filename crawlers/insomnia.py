"""Insomnia.gr classifieds scraping.

Fronted by Cloudflare, which challenges every navigation — _insomnia_goto
reloads once, then bails so a wedged challenge page never reaches
extract_insomnia_listings (whose page.evaluate() has no timeout and would
hang the whole loop forever). Also owns the wanted-ad (Ζήτηση) purge,
since that lives entirely inside insomnia's badge structure."""

import os
import re
import shutil
import time
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from listing_common import _norm, WANTED_KW, TRADE_KW
from config import PAGE_DELAY, INSOMNIA_GPU_URL, INSOMNIA_RAM_URL, RAM_LOG, GPU_LOG
from prices import parse_price
from cleaning import is_broken, clean_listings
from crawl_utils import (page_timeout, PageTimeoutException,
                         _known_streak_checker, new_unique, log_listings,
                         prune_urls, recreate_page)


def _insomnia_scroll_load(page) -> None:
    """Scroll the page to ensure all lazy-loaded listing cards are rendered."""
    try:
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.2)
        for _ in range(8):
            page.evaluate("window.scrollBy(0, 600)")
            time.sleep(0.15)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.3)
    except Exception as e:
        if "Target crashed" in str(e) or "Target page, context or browser has been closed" in str(e):
            raise RuntimeError("browser_crash") from e
        raise


def extract_insomnia_listings(page) -> list[dict]:
    """Parse listing cards from an insomnia.gr classifieds page."""
    # Bounded safety gate. `wait_for_selector` honors its timeout even on a wedged page, whereas
    # `_insomnia_scroll_load`'s page.evaluate() calls below have NO timeout and would hang the whole
    # watch loop forever if a missed Cloudflare challenge slipped past `_insomnia_goto`. If the
    # listing container isn't present within a few seconds, treat the page as wedged and bail.
    try:
        page.wait_for_selector('li[class*="insAdvertsList"]', timeout=8000)
    except Exception:
        print("  [insomnia] listings not present within 8s (wedged/blocked) — skipping", flush=True)
        return []
    _insomnia_scroll_load(page)
    listings = []
    for card in page.query_selector_all('li[class*="insAdvertsList"]'):
        try:
            price_el = card.query_selector("span.cFilePrice")
            price_raw = price_el.inner_text().strip() if price_el else ""
            price_raw = price_raw.replace("\xa0", " ").strip()
            price = parse_price(price_raw)

            link_el = card.query_selector('a[href*="/classifieds/item/"]')
            if not link_el:
                continue
            url = link_el.get_attribute("href") or ""

            title_attr = link_el.get_attribute("title") or ""
            m = re.search(r'"([^"]+)"', title_attr)
            name = m.group(1).strip() if m else title_attr.strip()
            if not name:
                continue

            cond_el   = card.query_selector(".insClassifiedsCondition")
            condition = cond_el.inner_text().strip() if cond_el else ""

            # Skip wanted ads (Ζήτηση badge / ΖΗΤΩ) and trades — the marker is a badge,
            # not in the title, so check the whole card. _norm() strips Greek accents:
            # "Ζήτηση".upper() is "ΖΉΤΗΣΗ" (accented Ή), which the old plain check missed.
            card_full = card.inner_text()
            card_norm = _norm(card_full)
            if any(k in card_norm for k in WANTED_KW) or any(k in card_norm for k in TRADE_KW):
                continue
            if is_broken(card_full):
                continue

            listings.append({"name": name, "condition": condition,
                              "price": price, "price_raw": price_raw, "url": url})
        except Exception:
            continue
    return listings


def insomnia_total_pages(page) -> int:
    try:
        el = page.query_selector("[data-ipspagination]")
        if el:
            v = el.get_attribute("data-pages")
            if v:
                return int(v)
    except Exception:
        pass
    return 1


def insomnia_page_url(base_url: str, n: int) -> str:
    if n <= 1:
        return base_url
    return f"{base_url.rstrip('/')}/page/{n}/"


def _insomnia_is_challenge(bpage) -> bool:
    """True if the current page is still a Cloudflare interstitial."""
    try:
        title = bpage.title().lower()
    except Exception:
        return True  # title() itself failing usually means a wedged challenge page
    return "just a moment" in title or "περιμένετε" in title or "challenge" in bpage.url


def _insomnia_goto(bpage, url: str, timeout: int = 60000) -> bool:
    """Navigate to an insomnia.gr URL. Return False if it timed out or is still a
    Cloudflare challenge after one reload — callers must NOT scroll/extract on False,
    since evaluate() on a wedged challenge page has no timeout and hangs forever."""
    try:
        bpage.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except PlaywrightTimeout:
        return False
    time.sleep(1.5)
    if _insomnia_is_challenge(bpage):
        time.sleep(8)
        try:
            bpage.reload(wait_until="domcontentloaded", timeout=30000)
            time.sleep(1.5)
        except Exception:
            return False
        # If the reload didn't clear the challenge, bail rather than hand a stuck
        # page to extract_insomnia_listings.
        if _insomnia_is_challenge(bpage):
            return False
    return True


def scan_page1_insomnia(bpage, url: str) -> list[dict]:
    if not _insomnia_goto(bpage, url, timeout=30000):
        print("  [scan] insomnia timeout — skipping", flush=True)
        return []
    return extract_insomnia_listings(bpage)


def initial_crawl_insomnia(bpage, already_known: set[str], base_url: str,
                           log_file: str, label: str,
                           log_filter=None, ctx=None, kind: str | None = None,
                           max_pages: int | None = None,
                           early_stop_after: int | None = None) -> set[str]:
    """Crawl pages of an insomnia.gr category using direct URL navigation.
    max_pages caps the page count; early_stop_after stops after that many
    consecutive already-known listings (see initial_crawl). Automatically
    resumes from the crashed page on browser crashes or hangs (requires ctx)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] ── {label} INITIAL CRAWL (insomnia.gr) ──────────────────")
    t0 = time.time()
    hit_old = _known_streak_checker(already_known, early_stop_after)

    all_listings: list[dict] = []
    consecutive_empty = 0
    total_pages = 999
    n = 1
    stop_early = False

    while not stop_early and n <= total_pages and (max_pages is None or n <= max_pages):
        if n > 1:
            time.sleep(PAGE_DELAY)
        try:
            with page_timeout(60):
                url = insomnia_page_url(base_url, n)
                ok = _insomnia_goto(bpage, url, timeout=40000)
                if not ok:
                    print(f"  Page {n:2}/{total_pages if total_pages != 999 else '?'}: timeout — skipping", flush=True)
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        print("  3 consecutive failures — stopping early", flush=True)
                        break
                    n += 1
                    continue

                # Successfully loaded page, update total_pages if needed
                pages_found = insomnia_total_pages(bpage)
                if pages_found > 1:
                    total_pages = pages_found

                listings = extract_insomnia_listings(bpage)

                if not listings:
                    print(f"  Page {n:2}/{total_pages}: empty, retrying…", flush=True)
                    time.sleep(2)
                    listings = extract_insomnia_listings(bpage)

                if not listings:
                    consecutive_empty += 1
                    try:
                        title_snippet = bpage.title()[:40]
                    except Exception:
                        title_snippet = "unknown"
                    print(f"  Page {n:2}/{total_pages}: still empty (title: {title_snippet})", flush=True)
                    if consecutive_empty >= 3:
                        print("  3 consecutive empty pages — stopping", flush=True)
                        break
                    n += 1
                    continue

                consecutive_empty = 0
                all_listings.extend(listings)
                print(f"  Page {n:2}/{total_pages}: {len(listings)} listings", flush=True)
                n += 1
                if hit_old(listings):
                    print(f"  Early stop: {early_stop_after} consecutive already-known listings", flush=True)
                    stop_early = True

        except (RuntimeError, PageTimeoutException) as e:
            is_timeout = isinstance(e, PageTimeoutException)
            if not is_timeout and ("browser_crash" not in str(e) or ctx is None):
                raise

            label_err = "timed out/hung" if is_timeout else "crashed"
            print(f"  [recovery] Browser {label_err} on page {n} — recreating page…", flush=True)
            try:
                bpage.close()
            except Exception:
                pass
            time.sleep(2)
            new_page = recreate_page(ctx)
            if new_page is None:
                print("  [recovery] Could not recreate page — stopping crawl early.", flush=True)
                break
            bpage = new_page
            print(f"  [recovery] Resuming from page {n}…", flush=True)
            # retry same page — don't increment n

    elapsed = time.time() - t0
    to_log = clean_listings(all_listings, kind) if kind else all_listings
    to_log = [item for item in to_log if not log_filter or log_filter(item)]
    new_listings = new_unique(to_log, already_known)
    if new_listings:
        log_listings(new_listings, ts, log_file)

    known = already_known | {item["url"] for item in to_log if item["url"]}
    skipped = len(to_log) - len(new_listings)
    filtered_out = len(all_listings) - len(to_log)
    print(f"\n  Done in {elapsed:.0f}s: {len(all_listings)} seen"
          + (f", {filtered_out} filtered/cleaned out" if filtered_out else "")
          + f", {len(new_listings)} new logged, {skipped} already in CSV")
    return known


# ── Wanted-ad purge (Ζήτηση listings) ─────────────────────────────────────────

def _card_is_wanted(card) -> bool:
    """True if an insomnia listing card is a Ζήτηση (wanted) or trade ad."""
    try:
        for el in card.query_selector_all(".ipsBadge, [class*='Badge']"):
            if _norm(el.inner_text() or "") == "ζητηση":
                return True
    except Exception:
        pass
    try:
        t = _norm(card.inner_text())
        return any(k in t for k in WANTED_KW) or any(k in t for k in TRADE_KW)
    except Exception:
        return False


def collect_wanted_insomnia(bpage, ctx, base_url: str, label: str) -> set[str]:
    """Crawl all pages of an insomnia category and return URLs of wanted/trade ads."""
    print(f"\n── Scanning {label} (insomnia) for Ζήτηση ads ──", flush=True)
    _insomnia_goto(bpage, base_url)
    total = insomnia_total_pages(bpage)
    wanted: set[str] = set()
    n = 1
    while n <= total:
        if n > 1:
            time.sleep(PAGE_DELAY)
            if not _insomnia_goto(bpage, insomnia_page_url(base_url, n), timeout=40000):
                n += 1
                continue
        try:
            _insomnia_scroll_load(bpage)
            cards = bpage.query_selector_all('li[class*="insAdvertsList"]')
            page_w = 0
            for card in cards:
                if _card_is_wanted(card):
                    link = card.query_selector('a[href*="/classifieds/item/"]')
                    href = (link.get_attribute("href") or "") if link else ""
                    if href.startswith("/"):
                        href = "https://www.insomnia.gr" + href
                    if href:
                        wanted.add(href); page_w += 1
            print(f"  Page {n:2}/{total}: {len(cards)} cards, {page_w} wanted", flush=True)
            n += 1
        except RuntimeError as e:
            if "browser_crash" in str(e):
                print(f"  [recovery] crash on page {n} — recreating page…", flush=True)
                time.sleep(2)
                np = recreate_page(ctx)
                if np is None:
                    break
                bpage = np
                continue
            raise
    return wanted


def purge_wanted(bpage, ctx) -> None:
    """One-shot: find all insomnia Ζήτηση (wanted) ads and remove them from the CSVs."""
    t0 = time.time()
    wanted = set()
    wanted |= collect_wanted_insomnia(bpage, ctx, INSOMNIA_GPU_URL, "GPU")
    wanted |= collect_wanted_insomnia(bpage, ctx, INSOMNIA_RAM_URL, "RAM")
    print(f"\nFound {len(wanted)} unique wanted/trade listings on insomnia.", flush=True)
    if not wanted:
        print("Nothing to prune.")
        return
    bdir = "backup_csv_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(bdir, exist_ok=True)
    total = 0
    for log_file in (RAM_LOG, GPU_LOG):
        if os.path.isfile(log_file):
            shutil.copy(log_file, os.path.join(bdir, log_file))
            removed = prune_urls(log_file, wanted)
            if removed:
                print(f"  {log_file}: removed {removed} wanted-ad row(s)")
            total += removed
    print(f"\nDone in {time.time()-t0:.0f}s: {total} CSV rows pruned. Backup: {bdir}")
