"""Facebook Marketplace scanner. Needs a logged-in session and infinite scroll.

The extraction, scrolling and login all live in fb_marketplace.py so there is
ONE implementation. This module wraps it for the watch layer:
  • _initial_crawl_facebook_gpu — one-shot seed (called by run_used_crawl).
  • _facebook_watch_worker      — daemon-thread body with its OWN Playwright
                                  instance (sync API is per-thread).

Both use `pipeline._process_new_listing` so the alert path is identical to the
main watch loop."""

import os
import threading
from datetime import datetime

import ai_verify
import applog
from config import FB_BLOCK_COOLDOWN, FB_GPU_LOG, FB_SCAN_INTERVAL
from crawl_utils import load_known_urls
from deals import is_gpu_deal
from pipeline import _process_new_listing

log = applog.get_logger()


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
    import time as _time
    t0 = _time.time()

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

    elapsed = _time.time() - t0
    note = "  (rate-limited mid-crawl — watch loop will retry later)" if fb_stats.get("blocked") else ""
    print(f"  Done in {elapsed:.0f}s: {len(new_items)} new GPU, "
          f"{len(fb_known)} total known{note}", flush=True)
    return fb_known


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
