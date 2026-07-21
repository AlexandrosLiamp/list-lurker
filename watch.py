"""Watch loop, crawl orchestrator, and every source-adapter the loop drives.

The category-table `watch_loop` iterates: each entry names a scan function
(scan_page1_*), a filter, and a deal-detection callable, so adding a new
source is one dict literal. run_used_crawl seeds those tables at startup by
running the same sources at whichever depth tier the CLI picked
(full / crawl / watch).

Vendora, Vinted, and Facebook adapters live here because they're pure
watch-loop plumbing on top of the per-source core modules
(vendora → requests+bs4, vinted → JSON API, fb → fb_marketplace's
logged-in browser).

The stall watchdog is a last-resort safety net: sync Playwright calls have
no timeout and can wedge a scan forever, so a separate thread notices the
loop has stopped heartbeating and execv()s the process."""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import ai_verify
import applog

from config import (
    RAM_URL, GPU_URL, CPU_URL, MOBO_URL,
    RAM_LOG, GPU_LOG, CPU_LOG, MOBO_LOG,
    INSOMNIA_GPU_URL, INSOMNIA_RAM_URL,
    VENDORA_GPU_URL, VENDORA_GPU_LOG, VENDORA_MAX_PAGES,
    VINTED_GPU_LOG, VINTED_CPU_LOG, VINTED_RAM_LOG, VINTED_MOBO_LOG,
    FB_GPU_LOG,
    GPU_RETAIL_URL, GPU_RETAIL_LOG,
    RAM_RETAIL_URL, RAM_RETAIL_LOG,
    CPU_RETAIL_URL, CPU_RETAIL_LOG,
    MOBO_RETAIL_URL, MOBO_RETAIL_LOG,
    LAPTOP_RETAIL_URL, LAPTOP_RETAIL_LOG,
    PAGE_DELAY, SCAN_INTERVAL,
    FB_SCAN_INTERVAL, FB_BLOCK_COOLDOWN, RETAIL_SCAN_INTERVAL,
    VINTED_MODE)
from prices import parse_price
from cleaning import clean_listings
from deals import match_gpu, is_real_gpu_card, is_gpu_deal
from alerts import send_discord, DISCORD_WEBHOOK
from crawl_utils import (load_known_urls, new_unique, _known_streak_checker,
                         log_listings, recreate_page)
from crawlers.skroutz import initial_crawl, scan_page1_skroutz
from crawlers.insomnia import initial_crawl_insomnia, scan_page1_insomnia
from retail import (crawl_retail, log_retail, log_retail_laptops,
                    save_retail_snapshot, detect_retail_drops, write_retail_deals)

log = applog.get_logger()


# ── Crawl-depth tiers ─────────────────────────────────────────────────────────
# Three tiers share one driver:
#   full  → every page of all used sources, plus the Skroutz retail catalogs.
#   crawl → stop a source after EARLY_STOP_KNOWN consecutive already-known
#           listings (feeds are newest-first). No retail.
#   watch → crawl the first WATCH_SEED_PAGES pages, then poll page 1 for new
#           listings + Discord alerts. No retail.

EARLY_STOP_KNOWN = 10     # consecutive already-known listings → stop the crawl
WATCH_SEED_PAGES = 3      # pages to crawl before the watch loop takes over

# depth tier → (max_pages, early_stop_after)
_DEPTH_PARAMS = {
    "full":  (None, None),
    "crawl": (None, EARLY_STOP_KNOWN),
    "watch": (WATCH_SEED_PAGES, None),
}


# ── Vendora.gr scanner ────────────────────────────────────────────────────────
# Vendora is scraped with requests + BeautifulSoup (no browser needed).

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


def scan_page1_vendora_gpu(_bpage, url: str) -> list[dict]:
    """Scan page 1 of Vendora GPU listings. _bpage is ignored (no browser needed)."""
    html = _vendora_fetch_page(url)
    if not html:
        print("  [vendora] fetch failed — skipping", flush=True)
        return []
    return _vendora_extract_listings(html)


def _initial_crawl_vendora_gpu(label: str, max_pages: int | None = None,
                               early_stop_after: int | None = None) -> set[str]:
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

    vendora_known = load_known_urls(VENDORA_GPU_LOG)
    hit_old = _known_streak_checker(set(vendora_known), early_stop_after)
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
                vendora_known.add(it["url"])

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


# ── Vinted.gr scanner ─────────────────────────────────────────────────────────
# Vinted is a used-goods marketplace scraped via its JSON API (see vinted.py).
# The API needs an anonymous session cookie, so we cache one requests.Session
# and reuse it across pages and watch cycles (self-refreshes on auth expiry).

_vinted_session = None
_vinted_enabled_cache = None   # auto-mode probe result, cached for the process


def _get_vinted_session(force_new: bool = False):
    global _vinted_session
    import vinted
    if _vinted_session is None or force_new:
        _vinted_session = vinted.make_session()
    return _vinted_session


def _reset_vinted_session():
    """Drop the cached session so the next fetch builds a fresh one. Closes the
    documented robustness gap: the cached anon session was never recreated within a
    run, so a session that went bad mid-watch stayed bad until the process restarted."""
    global _vinted_session
    _vinted_session = None


def vinted_enabled(*, reprobe: bool = False) -> bool:
    """Resolve whether Vinted should run, honouring VINTED_MODE (see config):
      "on"  → always True   ·   "off" → always False   ·   "auto" → probe once.
    In auto mode the API is probed a single time (result cached for the process) and
    Vinted is enabled only if this IP can currently reach it, printing one notice
    either way. This self-heals across the Cloudflare IP-reputation block: enabled
    automatically when the IP is clean, muted (no failure spam) when it's blocked."""
    global _vinted_enabled_cache
    if VINTED_MODE == "on":
        return True
    if VINTED_MODE == "off":
        return False
    if _vinted_enabled_cache is not None and not reprobe:
        return _vinted_enabled_cache
    import vinted
    ok = vinted.probe(session=_get_vinted_session())
    _vinted_enabled_cache = ok
    if ok:
        print("[vinted] auto: API reachable — enabled this run.", flush=True)
    else:
        print("[vinted] auto: API unreachable (Cloudflare IP block?) — disabled this "
              "run. Set VINTED_ENABLED=on to force, =off to silence.", flush=True)
    return ok


def scan_page1_vinted(_bpage, url: str) -> list[dict]:
    """Scan page 1 of a Vinted catalog (part inferred from the URL's catalog id).
    _bpage is ignored — Vinted uses the JSON API, not the browser."""
    import vinted
    listings = vinted.fetch_by_url(url, page=1, session=_get_vinted_session())
    if not listings:
        # Page 1 of a 96-item catalog is never legitimately empty, so an empty result
        # means the fetch failed — drop the session so next cycle rebuilds it (self-heal).
        print("  [vinted] empty/fetch failed — recreating session for next cycle", flush=True)
        _reset_vinted_session()
    return listings


def _initial_crawl_vinted(part: str, log_file: str, max_pages: int | None = None,
                          early_stop_after: int | None = None) -> set[str]:
    """One-shot: crawl a Vinted catalog (gpu/cpu/ram/mobo) and save to CSV. Returns
    the known URL set for the watch loop. max_pages caps pages; early_stop_after
    stops after that many consecutive already-known listings (feed is newest-first).
    GPU pages are additionally filtered to recognised models, like the other GPU
    sources; the builder parts (cpu/ram/mobo) log every clean listing."""
    import vinted
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] ── VINTED {part.upper()}: initial crawl ────────────────")
    t0 = time.time()

    known = load_known_urls(log_file)
    hit_old = _known_streak_checker(set(known), early_stop_after)
    session = _get_vinted_session()
    cid = vinted.CATALOG_IDS[part]
    is_gpu = (part == "gpu")
    page_num = 1
    consecutive_empty = 0

    while True:
        if max_pages is not None and page_num > max_pages:
            print(f"  Stopping after {max_pages} page(s) (max_pages limit)", flush=True)
            break
        time.sleep(PAGE_DELAY if page_num > 1 else 0)
        listings = vinted.fetch_page(cid, page=page_num, session=session)
        if not listings:
            consecutive_empty += 1
            print(f"  Page {page_num:2}: empty/fetch failed", flush=True)
            if consecutive_empty >= 3:
                print("  3 consecutive empty pages — stopping.", flush=True)
                break
            page_num += 1
            continue

        consecutive_empty = 0
        # For GPU, restrict to recognised models before early-stop/logging. Check
        # early-stop against the pre-run known set BEFORE `known` is mutated below.
        filtered = ([it for it in listings if match_gpu(it["name"]) is not None and is_real_gpu_card(it["name"])]
                    if is_gpu else listings)
        stop_early = hit_old(filtered)
        clean = clean_listings(filtered, part)
        clean = new_unique(clean, known)
        if clean:
            log_listings(clean, ts, log_file)
            for it in clean:
                known.add(it["url"])

        print(f"  Page {page_num:2}: {len(listings)} listings, "
              f"{len(clean)} new {part}", flush=True)
        page_num += 1
        if stop_early:
            print(f"  Early stop: {early_stop_after} consecutive already-known listings", flush=True)
            break

    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.0f}s: {len(known)} total known URLs", flush=True)
    return known


# ── Facebook Marketplace scanner ──────────────────────────────────────────────
# Facebook needs a logged-in session and infinite scroll. The extraction,
# scrolling and login all live in fb_marketplace.py so there is ONE
# implementation; here we build a dedicated FB browser context (seeded with the
# saved session) and append new GPUs to fb_gpu.csv.


def _initial_crawl_facebook_gpu(ctx, mode: str = "full") -> set[str]:
    """Multi-query Facebook Marketplace GPU crawl. mode='full' scrolls each query to
    the bottom (the `crawl full` tier); mode='watch' stops once it reaches already-
    known listings, like the watch loop (the early-stop `crawl` tier). Runs in a
    dedicated FB context (seeded from fb_state.json) created off the shared browser,
    so it stays isolated from the Skroutz/Insomnia scraping. Returns the set of known
    FB GPU URLs for the watch loop to reuse."""
    import fb_marketplace

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] ── Facebook Marketplace GPU scan ({mode}) ─────────────────────────")
    if not os.path.isfile(fb_marketplace.STATE_FILE):
        print("  ⚠ No saved FB session (fb_state.json) — results will be limited.\n"
              "    Run once:  python fb_marketplace.py --login", flush=True)
    t0 = time.time()

    fb_known = load_known_urls(FB_GPU_LOG)
    fb_ctx = fb_marketplace.make_fb_context(ctx.browser)
    fb_page = fb_ctx.new_page()
    fb_stats = {}
    try:
        # crawl_facebook_gpu updates fb_known in place and returns only the new rows.
        new_items = fb_marketplace.crawl_facebook_gpu(
            fb_page, known=fb_known, ts=ts, mode=mode, stats=fb_stats)
    finally:
        fb_ctx.close()
    if new_items:
        fb_marketplace.log_listings(new_items, FB_GPU_LOG)  # writes the 7-col fb_gpu schema

    elapsed = time.time() - t0
    note = "  (rate-limited mid-crawl — watch loop will retry later)" if fb_stats.get("blocked") else ""
    print(f"  Done in {elapsed:.0f}s: {len(new_items)} new GPU, "
          f"{len(fb_known)} total known{note}", flush=True)
    return fb_known


# ── Per-listing pipeline ──────────────────────────────────────────────────────

def _process_new_listing(item, *, kind, deal_fn, bpage, ai_client,
                         notified, verified):
    """Print one new listing, run the deal check, optionally AI-verify it, and fire
    a Discord alert if it qualifies. Shared by every watch source (Skroutz/Insomnia/
    Vendora categories and the Facebook scan) so the alert pipeline lives in one
    place. `notified`/`verified` are the loop-level dedupe sets, mutated in place."""
    reason, ppr = deal_fn(item)
    price_str = f"{item['price']:.2f} €" if item["price"] else "?"
    deal_tag  = f"  *** DEAL: {reason} ***" if reason else ""
    cond      = f"[{item['condition'][:20]}]" if item["condition"] else ""
    print(f"    {price_str:>10} {cond:<22} {item['name'][:50]}{deal_tag}", flush=True)

    if not reason or item["url"] in notified:
        return

    # Default alert target = the listing as scraped
    targets = [(item, reason, ppr)]

    # ── Layer 2: AI verification (GPU deals only — only GPU produces a `reason`) ──
    # Open the real listing and let the model say what the item actually IS. This is
    # the bulletproof backstop for anything Layer 1 missed: a laptop / prebuilt PC /
    # mobile GPU / accessory that merely *mentions* a GPU model gets suppressed here.
    # If the model is unavailable or errors, we fall back to alerting (Layer 1 already
    # vetted the name) rather than going silent.
    if (kind == "gpu" and ai_client is not None and bpage is not None
            and item["url"] not in verified):
        verified.add(item["url"])
        try:
            verdict = ai_verify.verify_gpu_card(bpage, item["url"], item["name"])
        except Exception as e:
            verdict = None
            print(f"    ↳ [ai] verify error: {str(e)[:90]}", flush=True)

        if verdict is not None:
            if not verdict.available:
                print("    ↳ [ai] listing is sold/closed — alert suppressed", flush=True)
                notified.add(item["url"])
                return
            if not verdict.is_card:
                print(f"    ↳ [ai] NOT a standalone GPU card (category="
                      f"{verdict.category!r}) — alert suppressed", flush=True)
                notified.add(item["url"])
                return
            # AI confirms a real card. If it read a different asking price, re-check the
            # deal at that price (catches deposit/teaser prices that aren't real deals).
            if verdict.price and abs(verdict.price - (item["price"] or 0)) > 1:
                pseudo = {**item, "price": verdict.price}
                r2, p2 = deal_fn(pseudo)
                if not r2:
                    print(f"    ↳ [ai] corrected price {verdict.price:.0f}€ is no longer "
                          f"a deal — suppressed", flush=True)
                    notified.add(item["url"])
                    return
                targets = [(pseudo, r2, p2)]
                print(f"    ↳ [ai] verified GPU card; price corrected → {verdict.price:.0f}€",
                      flush=True)
            else:
                print(f"    ↳ [ai] verified standalone GPU card "
                      f"(category={verdict.category!r})", flush=True)

    for tgt, tgt_reason, tgt_ppr in targets:
        extra = []
        if tgt_ppr is not None:
            extra = [{"name": "PPR", "value": f"{tgt_ppr:.3f}", "inline": True}]
        send_discord(tgt, tgt_reason, extra_fields=extra)
        print(f"    ↳ Discord alert {'sent' if DISCORD_WEBHOOK else '(no webhook)'}", flush=True)
    notified.add(item["url"])


# ── Facebook watch thread ─────────────────────────────────────────────────────

def _facebook_watch_worker(facebook_known: set[str], stop_event: threading.Event,
                           ai_client=None, verbose: bool = True) -> None:
    """Daemon-thread body for Facebook watching. Owns its OWN Playwright + browser
    (the sync API is per-thread, so a separate instance here is the safe way to run
    FB off the main thread). This decouples Facebook's slow initial seed from the main
    watch loop so the fast sources start polling immediately.

    Lifecycle each round:
      1) crawl FB (watch mode — scroll until known).
      2) FIRST round = a silent SEED: populate `facebook_known` + fb_gpu.csv, NO alerts
         (mirrors how the other sources seed before the watch loop, so a stale CSV can't
         spam Discord on startup).
      3) Later rounds: Discord-alert genuinely-new GPU deals.
      4) Sleep FB_SCAN_INTERVAL, or FB_BLOCK_COOLDOWN after an anti-bot block.
    Uses its own `fb_notified` dedupe set (FB URLs are distinct from other sources, so
    no locking needed against the main loop)."""
    import fb_marketplace
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    fb_notified: set[str] = set()
    fb_verified: set[str] = set()
    fb_deal_fn = lambda it: is_gpu_deal(it) or (None, None)
    seeded = False

    try:
        with Stealth().use_sync(sync_playwright()) as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--disable-blink-features=AutomationControlled"])
            fb_ctx = fb_marketplace.make_fb_context(browser)
            fb_page = fb_ctx.new_page()
            # A second page in the same (logged-in) FB context used by Layer-2
            # ai_verify to open each deal candidate's listing — keeps the search/
            # scroll page undisturbed.
            fb_verify_page = fb_ctx.new_page()

            while not stop_event.is_set():
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                stats: dict = {}
                print(f"[{ts}] [facebook] {'initial seed (no alerts)' if not seeded else 'scan'}…",
                      flush=True)
                try:
                    fb_new = fb_marketplace.crawl_facebook_gpu(
                        fb_page, known=facebook_known, ts=ts, mode="watch",
                        verbose=verbose, stats=stats)
                except Exception as e:
                    print(f"  [facebook] scan error: {str(e)[:120]}", flush=True)
                    log.exception("facebook watch scan failed")
                    # Rebuild the page/context and retry after the normal interval.
                    try: fb_ctx.close()
                    except Exception: pass
                    try:
                        fb_ctx = fb_marketplace.make_fb_context(browser)
                        fb_page = fb_ctx.new_page()
                        fb_verify_page = fb_ctx.new_page()
                    except Exception:
                        log.exception("facebook context rebuild failed — stopping FB thread")
                        return
                    stop_event.wait(FB_SCAN_INTERVAL)
                    continue

                if fb_new:
                    fb_marketplace.log_listings(fb_new, FB_GPU_LOG)
                    if seeded:
                        print(f"  {len(fb_new)} NEW Facebook GPU:", flush=True)
                        for item in fb_new:
                            # Layer 2: AI-verify each FB deal candidate on its own
                            # listing page (logged-in FB context) before alerting.
                            _process_new_listing(item, kind="gpu", deal_fn=fb_deal_fn,
                                                 bpage=fb_verify_page, ai_client=ai_client,
                                                 notified=fb_notified, verified=fb_verified)
                    else:
                        print(f"  seeded {len(fb_new)} Facebook GPU listings (no alerts "
                              f"on first round).", flush=True)
                elif not stats.get("blocked"):
                    print("  0 new Facebook listings.", flush=True)

                seeded = True
                if stats.get("blocked"):
                    print(f"  [facebook] backing off {FB_BLOCK_COOLDOWN // 60} min after "
                          f"anti-bot block.", flush=True)
                    stop_event.wait(FB_BLOCK_COOLDOWN)
                else:
                    stop_event.wait(FB_SCAN_INTERVAL)

            try:
                fb_ctx.close(); browser.close()
            except Exception:
                pass
    except Exception:
        log.exception("facebook watch thread crashed")


# ── Auto-snipe launcher ───────────────────────────────────────────────────────

_snipe_proc = None


def _maybe_autosnipe():
    """If auto_snipe is enabled (the dashboard toggle in negotiator_config.json), launch a
    background negotiator snipe pass — it sends lowball offers on new Skoop GPU listings that
    pass the configured filters + AI gate. Guarded so only one runs at a time; never raises
    into the watch loop."""
    global _snipe_proc
    try:
        import json as _json
        cfg = _json.load(open("negotiator_config.json", encoding="utf-8"))
        if not cfg.get("auto_snipe"):
            return
        if _snipe_proc is not None and _snipe_proc.poll() is None:
            return  # previous snipe still running — don't stack
        _snipe_proc = subprocess.Popen(
            [sys.executable, "negotiator.py", "snipe", "--confirm"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("  [auto-snipe] launched a snipe pass (new passing listings get an offer).", flush=True)
    except Exception as e:
        print(f"  [auto-snipe] skipped: {str(e)[:100]}", flush=True)


# ── Stall watchdog ────────────────────────────────────────────────────────────
# Playwright's sync page.evaluate() has no timeout: a wedged page (missed Cloudflare
# challenge, stuck renderer) blocks the calling thread forever — not an exception, so the
# loop's try/except can't catch it, and you can't interrupt a sync Playwright call from
# another thread. The universal safety net is a separate watchdog thread that notices the
# loop stopped making progress and restarts the process, which kills the wedged browser and
# resumes (re-seeds, skips already-known listings). The per-source wait_for_selector gates
# make real hangs rare, so this is a last resort.
_LAST_PROGRESS = time.time()
_BOOT = time.time()


def _heartbeat() -> None:
    global _LAST_PROGRESS
    _LAST_PROGRESS = time.time()


def _maybe_reset_restart_budget() -> None:
    """After this process has run healthily for a while, clear the inherited restart counter so
    isolated hangs hours apart don't accumulate toward the give-up budget."""
    if int(os.environ.get("WATCH_RESTARTS", "0")) and time.time() - _BOOT > 1800:
        os.environ["WATCH_RESTARTS"] = "0"


def _stall_watchdog(stall_limit: int = 180, check_every: int = 20) -> None:
    while True:
        time.sleep(check_every)
        stalled = time.time() - _LAST_PROGRESS
        if stalled <= stall_limit:
            continue
        # avoid a tight restart loop if a source hangs immediately on every boot
        restarts = int(os.environ.get("WATCH_RESTARTS", "0"))
        msg = (f"[watchdog] watch loop made no progress for {stalled:.0f}s — a scan is hung "
               f"(likely a wedged Cloudflare page). ")
        if restarts >= 6:
            print(msg + "Already restarted 6×; staying up so it's visible. Check the logs / "
                  "restart manually.", flush=True)
            try:
                log.error("watchdog: stalled %.0fs but restart budget exhausted", stalled)
            except Exception:
                pass
            return
        print(msg + f"Restarting the process to recover (restart #{restarts + 1})…", flush=True)
        try:
            log.error("watchdog restart #%d: stalled %.0fs", restarts + 1, stalled)
        except Exception:
            pass
        sys.stdout.flush()
        sys.stderr.flush()
        os.environ["WATCH_RESTARTS"] = str(restarts + 1)
        # execv replaces this process image; the open pipes to the Playwright driver close,
        # so the wedged chromium is torn down and the fresh process starts clean.
        os.execv(sys.executable, [sys.executable] + sys.argv)


# ── Watch loop ────────────────────────────────────────────────────────────────

def watch_loop(bpage, ctx, ram_known: set[str], gpu_known: set[str],
               cpu_known: set[str] | None = None, mobo_known: set[str] | None = None,
               do_ram: bool = True, do_gpu: bool = True,
               do_cpu: bool = False, do_mobo: bool = False,
               do_laptop: bool = False,
               do_vendora_gpu: bool = False,
               vendora_gpu_known: set[str] | None = None,
               do_facebook: bool = False,
               facebook_known: set[str] | None = None,
               do_vinted: bool = False,
               vinted_known: dict | None = None,
               do_retail: bool = False,
               ai_client=None) -> None:
    notified: set[str] = set()
    verified: set[str] = set()          # URLs already AI-verified (avoid re-paying)
    consecutive_crashes = 0
    cpu_known  = cpu_known  if cpu_known  is not None else set()
    mobo_known = mobo_known if mobo_known is not None else set()
    vendora_gpu_known = vendora_gpu_known if vendora_gpu_known is not None else set()
    facebook_known = facebook_known if facebook_known is not None else set()
    vinted_known = vinted_known if vinted_known is not None else {}
    _vk = lambda p: vinted_known.setdefault(p, set())  # per-part known set
    if do_vinted and not vinted_enabled():
        # vinted_enabled() prints its own notice (auto probe outcome / forced off).
        do_vinted = False
    import vinted

    all_categories = [
        {
            "label":      "Skroutz RAM",
            "kind":       "ram",
            "enabled":    do_ram,
            "url":        RAM_URL,
            "log_file":   RAM_LOG,
            "known":      ram_known,
            "scan_fn":    scan_page1_skroutz,
            "log_filter": None,
            "deal_fn":    lambda item: (None, None),  # RAM alerts disabled
        },
        {
            "label":      "Skroutz GPU",
            "kind":       "gpu",
            "enabled":    do_gpu,
            "url":        GPU_URL,
            "log_file":   GPU_LOG,
            "known":      gpu_known,
            "scan_fn":    scan_page1_skroutz,
            "log_filter": lambda item: match_gpu(item["name"]) is not None and is_real_gpu_card(item["name"]),
            "deal_fn":    lambda item: is_gpu_deal(item) or (None, None),
        },
        {
            "label":      "Insomnia RAM",
            "kind":       "ram",
            "enabled":    do_ram,
            "url":        INSOMNIA_RAM_URL,
            "log_file":   RAM_LOG,
            "known":      ram_known,   # shared with Skroutz RAM (same CSV) — no re-logging
            "scan_fn":    scan_page1_insomnia,
            "log_filter": None,
            "deal_fn":    lambda item: (None, None),  # RAM alerts disabled
        },
        {
            "label":      "Insomnia GPU",
            "kind":       "gpu",
            "enabled":    do_gpu,
            "url":        INSOMNIA_GPU_URL,
            "log_file":   GPU_LOG,
            "known":      gpu_known,   # shared with Skroutz GPU (same CSV) — no re-logging
            "scan_fn":    scan_page1_insomnia,
            "log_filter": lambda item: match_gpu(item["name"]) is not None and is_real_gpu_card(item["name"]),
            "deal_fn":    lambda item: is_gpu_deal(item) or (None, None),
        },
        {
            "label":      "Vendora GPU",
            "kind":       "gpu",
            "enabled":    do_vendora_gpu,
            "url":        _vendora_page_url(1),
            "log_file":   VENDORA_GPU_LOG,
            "known":      vendora_gpu_known,
            "scan_fn":    scan_page1_vendora_gpu,
            "log_filter": lambda item: match_gpu(item["name"]) is not None and is_real_gpu_card(item["name"]),
            "deal_fn":    lambda item: is_gpu_deal(item) or (None, None),
        },
        {
            "label":      "Skroutz CPU",
            "kind":       "cpu",
            "enabled":    do_cpu,
            "url":        CPU_URL,
            "log_file":   CPU_LOG,
            "known":      cpu_known,
            "scan_fn":    scan_page1_skroutz,
            "log_filter": None,
            "deal_fn":    lambda item: (None, None),  # no CPU alerts (builder only)
        },
        {
            "label":      "Skroutz Motherboard",
            "kind":       "mobo",
            "enabled":    do_mobo,
            "url":        MOBO_URL,
            "log_file":   MOBO_LOG,
            "known":      mobo_known,
            "scan_fn":    scan_page1_skroutz,
            "log_filter": None,
            "deal_fn":    lambda item: (None, None),  # no mobo alerts (builder only)
        },
        # ── Vinted (used-goods marketplace, JSON API) ──
        {
            "label":      "Vinted GPU",
            "kind":       "gpu",
            "enabled":    do_vinted and do_gpu,
            "url":        vinted.catalog_url("gpu"),
            "log_file":   VINTED_GPU_LOG,
            "known":      _vk("gpu"),
            "scan_fn":    scan_page1_vinted,
            "log_filter": lambda item: match_gpu(item["name"]) is not None and is_real_gpu_card(item["name"]),
            "deal_fn":    lambda item: is_gpu_deal(item) or (None, None),
        },
        {
            "label":      "Vinted CPU",
            "kind":       "cpu",
            "enabled":    do_vinted and do_cpu,
            "url":        vinted.catalog_url("cpu"),
            "log_file":   VINTED_CPU_LOG,
            "known":      _vk("cpu"),
            "scan_fn":    scan_page1_vinted,
            "log_filter": None,
            "deal_fn":    lambda item: (None, None),  # no CPU alerts (builder only)
        },
        {
            "label":      "Vinted RAM",
            "kind":       "ram",
            "enabled":    do_vinted and do_ram,
            "url":        vinted.catalog_url("ram"),
            "log_file":   VINTED_RAM_LOG,
            "known":      _vk("ram"),
            "scan_fn":    scan_page1_vinted,
            "log_filter": None,
            "deal_fn":    lambda item: (None, None),  # RAM alerts disabled
        },
        {
            "label":      "Vinted Motherboard",
            "kind":       "mobo",
            "enabled":    do_vinted and do_mobo,
            "url":        vinted.catalog_url("mobo"),
            "log_file":   VINTED_MOBO_LOG,
            "known":      _vk("mobo"),
            "scan_fn":    scan_page1_vinted,
            "log_filter": None,
            "deal_fn":    lambda item: (None, None),  # no mobo alerts (builder only)
        },
    ]
    categories = [c for c in all_categories if c["enabled"]]

    # Retail catalogs to periodically refresh — only when explicitly enabled.
    # Retail is otherwise a manual operation (`crawl full` / `crawl skroutz`),
    # so the watch tier leaves it off.
    retail_jobs = []
    if do_retail:
        if do_gpu:  retail_jobs.append((GPU_RETAIL_URL,  GPU_RETAIL_LOG,  "GPU",         "gpu"))
        if do_ram:  retail_jobs.append((RAM_RETAIL_URL,  RAM_RETAIL_LOG,  "RAM",         "ram"))
        if do_cpu:  retail_jobs.append((CPU_RETAIL_URL,  CPU_RETAIL_LOG,  "CPU",         "cpu"))
        if do_mobo: retail_jobs.append((MOBO_RETAIL_URL, MOBO_RETAIL_LOG, "Motherboard", "mobo"))
        if do_laptop: retail_jobs.append((LAPTOP_RETAIL_URL, LAPTOP_RETAIL_LOG, "Laptop", "laptop"))

    last_retail_scan = time.time()

    # Facebook is heavy (15 scrolled queries, slow initial seed) and rate-limit prone,
    # so it runs in its OWN daemon thread with its OWN browser. That way the slow FB
    # seed never blocks the fast sources below — they start polling right away, and
    # FB seeds/alerts independently in the background. See _facebook_watch_worker.
    fb_stop = None
    if do_facebook:
        fb_stop = threading.Event()
        threading.Thread(
            target=_facebook_watch_worker, args=(facebook_known, fb_stop, ai_client),
            name="facebook-watch", daemon=True).start()
        print("  [facebook] started in a background thread — seeding without blocking "
              "the fast sources.", flush=True)

    # Watchdog: if any scan hangs the loop, restart the process to recover (see _stall_watchdog).
    _heartbeat()
    threading.Thread(target=_stall_watchdog, name="stall-watchdog", daemon=True).start()

    while True:
        _heartbeat()
        time.sleep(SCAN_INTERVAL)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Retail rescan (all active catalogs)
        if retail_jobs and time.time() - last_retail_scan >= RETAIL_SCAN_INTERVAL:
            retail_log_files = {j[1] for j in retail_jobs}
            if GPU_RETAIL_LOG in retail_log_files:
                save_retail_snapshot(GPU_RETAIL_LOG)
            if RAM_RETAIL_LOG in retail_log_files:
                save_retail_snapshot(RAM_RETAIL_LOG)
            # None = category wasn't successfully re-scanned; write_retail_deals
            # will keep whatever's already in retail_deals.json for it, so a failed
            # scan can't wipe drops the other category legitimately surfaced.
            w_gpu_drops, w_ram_drops = None, None
            for url, log_file, label, kind in retail_jobs:
                _heartbeat()   # retail crawls are multi-page; keep the watchdog from false-firing
                try:
                    items = crawl_retail(bpage, url, label, kind=kind)
                    if items:
                        if kind == "laptop":
                            log_retail_laptops(items, ts, log_file)
                        else:
                            log_retail(items, ts, log_file)
                        # Only diff against the snapshot when the crawl actually
                        # produced data — an empty result (blocked page, no cards)
                        # means CSV is unchanged, so detect_retail_drops would
                        # return [] and wipe the file's other side.
                        if kind == "gpu":
                            w_gpu_drops = detect_retail_drops(GPU_RETAIL_LOG)
                        elif kind == "ram":
                            w_ram_drops = detect_retail_drops(RAM_RETAIL_LOG)
                except Exception as e:
                    print(f"  [retail {label}] Scan error: {e}", flush=True)
                    log.exception("retail scan failed for %s", label)
            last_retail_scan = time.time()
            if w_gpu_drops is not None or w_ram_drops is not None:
                write_retail_deals(w_gpu_drops, w_ram_drops)
                if w_gpu_drops:
                    print(f"  [deals] {len(w_gpu_drops)} GPU price drops detected")
                if w_ram_drops:
                    print(f"  [deals] {len(w_ram_drops)} RAM price drops detected")

        # (Facebook is handled by its own background thread — see above.)

        for cat in categories:
            _heartbeat()   # if scan_fn below hangs, the heartbeat stops → watchdog recovers
            print(f"[{ts}] Scanning {cat['label']} page 1…", end=" ", flush=True)
            try:
                listings = cat["scan_fn"](bpage, cat["url"])
                consecutive_crashes = 0
            except Exception as e:
                err = str(e)
                print(f"error: {err[:120]}", flush=True)
                log.exception("scan failed for %s", cat["label"])  # full traceback → log

                if "crashed" in err.lower() or "target closed" in err.lower():
                    consecutive_crashes += 1
                    print(f"  [recovery] Page crashed ({consecutive_crashes}x) — recreating…", flush=True)
                    time.sleep(3)
                    new_page = recreate_page(ctx)
                    if new_page:
                        bpage = new_page
                    else:
                        print("  [recovery] Could not recover — skipping this cycle.", flush=True)
                continue

            # Drop dirty/ambiguous listings, then filter GPU to recognised models
            if cat.get("kind"):
                listings = clean_listings(listings, cat["kind"])
            if cat["log_filter"]:
                listings = [item for item in listings if cat["log_filter"](item)]

            new = new_unique(listings, cat["known"])

            if not new:
                print(f"{len(listings)} listings, 0 new.", flush=True)
                continue

            log_listings(new, ts, cat["log_file"])
            for item in new:
                cat["known"].add(item["url"])

            print(f"{len(listings)} listings, {len(new)} NEW:", flush=True)
            for item in new:
                _process_new_listing(item, kind=cat["kind"], deal_fn=cat["deal_fn"],
                                     bpage=bpage, ai_client=ai_client,
                                     notified=notified, verified=verified)

        # Auto-snipe: when enabled, fire lowball offers on new passing Skoop GPU listings.
        _maybe_autosnipe()
        _heartbeat()                    # completed a full healthy cycle
        _maybe_reset_restart_budget()   # forgive old restarts once we've been stable a while


# ── Crawl orchestrator ────────────────────────────────────────────────────────

def _gpu_logf(item):
    return match_gpu(item["name"]) is not None and is_real_gpu_card(item["name"])


def run_used_crawl(bpage, ctx, parts, depth, sources=None, skip_facebook=False):
    """Crawl the used-marketplace sources for `parts` at the given depth tier
    ('full' | 'crawl' | 'watch'). `sources` optionally restricts which sources
    run, e.g. {'facebook'}. `skip_facebook` omits the (slow) synchronous Facebook
    seed — the watch path uses this because Facebook is seeded in its own background
    thread instead (see _facebook_watch_worker). Returns the known-URL sets used to
    seed the watch loop: {'ram','gpu','cpu','mobo','vendora_gpu','facebook','vinted_<part>'}."""
    max_pages, early = _DEPTH_PARAMS[depth]
    # Facebook scrolls fully on the `full` tier; on the early-stop `crawl` tier (and
    # while seeding the watch loop) it stops once it reaches already-known listings.
    fb_mode = "full" if depth == "full" else "watch"
    def want(src):
        if not (sources is None or src in sources):
            return False
        # Vinted runs only when the auto-gate (or a forced VINTED_MODE) allows it.
        # vinted_enabled() probes the API once and caches the result, so this prints
        # a single notice even though want("vinted") is called per part.
        if src == "vinted" and not vinted_enabled():
            return False
        return True
    known = {"ram": set(), "gpu": set(), "cpu": set(), "mobo": set(),
             "vendora_gpu": set(), "facebook": set(),
             "vinted_gpu": set(), "vinted_cpu": set(),
             "vinted_ram": set(), "vinted_mobo": set()}

    if "ram" in parts:
        k = load_known_urls(RAM_LOG)
        if want("skoop"):
            k = initial_crawl(bpage, k, RAM_URL, RAM_LOG, "Skroutz RAM", kind="ram",
                              max_pages=max_pages, early_stop_after=early)
        if want("insomnia"):
            k = initial_crawl_insomnia(bpage, k, INSOMNIA_RAM_URL, RAM_LOG, "Insomnia RAM",
                                       ctx=ctx, kind="ram", max_pages=max_pages,
                                       early_stop_after=early)
        known["ram"] = k
        if want("vinted"):
            known["vinted_ram"] = _initial_crawl_vinted(
                "ram", VINTED_RAM_LOG, max_pages=max_pages, early_stop_after=early)

    if "gpu" in parts:
        k = load_known_urls(GPU_LOG)
        if want("skoop"):
            k = initial_crawl(bpage, k, GPU_URL, GPU_LOG, "Skroutz GPU", kind="gpu",
                              log_filter=_gpu_logf, max_pages=max_pages, early_stop_after=early)
        if want("insomnia"):
            k = initial_crawl_insomnia(bpage, k, INSOMNIA_GPU_URL, GPU_LOG, "Insomnia GPU",
                                       log_filter=_gpu_logf, ctx=ctx, kind="gpu",
                                       max_pages=max_pages, early_stop_after=early)
        known["gpu"] = k
        if want("vendora"):
            known["vendora_gpu"] = _initial_crawl_vendora_gpu(
                "gpu", max_pages=max_pages, early_stop_after=early)
        if want("facebook") and not skip_facebook:
            known["facebook"] = _initial_crawl_facebook_gpu(ctx, mode=fb_mode)
        if want("vinted"):
            known["vinted_gpu"] = _initial_crawl_vinted(
                "gpu", VINTED_GPU_LOG, max_pages=max_pages, early_stop_after=early)

    if "cpu" in parts:
        if want("skoop"):
            k = load_known_urls(CPU_LOG)
            known["cpu"] = initial_crawl(bpage, k, CPU_URL, CPU_LOG, "Skroutz CPU", kind="cpu",
                                         max_pages=max_pages, early_stop_after=early)
        if want("vinted"):
            known["vinted_cpu"] = _initial_crawl_vinted(
                "cpu", VINTED_CPU_LOG, max_pages=max_pages, early_stop_after=early)

    if "mobo" in parts:
        if want("skoop"):
            k = load_known_urls(MOBO_LOG)
            known["mobo"] = initial_crawl(bpage, k, MOBO_URL, MOBO_LOG, "Skroutz Motherboard",
                                          kind="mobo", max_pages=max_pages, early_stop_after=early)
        if want("vinted"):
            known["vinted_mobo"] = _initial_crawl_vinted(
                "mobo", VINTED_MOBO_LOG, max_pages=max_pages, early_stop_after=early)

    return known
