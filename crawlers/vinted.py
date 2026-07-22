"""Vinted.gr scanner — used-goods marketplace via its JSON API (vinted.py, top level).

The API needs an anonymous session cookie, so we cache one requests.Session and
reuse it across pages and watch cycles. Vinted also has an "auto" enable mode:
we probe the API once (cached for the process) and skip Vinted entirely if the
IP is under a Cloudflare block, so a rotating IP self-heals on the next start.
"""

import time
from datetime import datetime

from cleaning import clean_listings
from config import PAGE_DELAY, VINTED_MODE
from crawl_utils import (_known_streak_checker, load_known_prices, log_listings,
                         new_unique)
from deals import is_real_gpu_card, match_gpu


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
                          early_stop_after: int | None = None) -> dict[str, float | None]:
    """One-shot: crawl a Vinted catalog (gpu/cpu/ram/mobo) and save to CSV. Returns
    the known URL set for the watch loop. max_pages caps pages; early_stop_after
    stops after that many consecutive already-known listings (feed is newest-first).
    GPU pages are additionally filtered to recognised models, like the other GPU
    sources; the builder parts (cpu/ram/mobo) log every clean listing."""
    import vinted
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] ── VINTED {part.upper()}: initial crawl ────────────────")
    t0 = time.time()

    known = load_known_prices(log_file)
    # Frozen snapshot (same reason as vendora): early-stop must judge against the
    # pre-run state while `known` keeps growing in the page loop below.
    hit_old = _known_streak_checker(dict(known), early_stop_after)
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
                known[it["url"]] = it.get("price")

        print(f"  Page {page_num:2}: {len(listings)} listings, "
              f"{len(clean)} new {part}", flush=True)
        page_num += 1
        if stop_early:
            print(f"  Early stop: {early_stop_after} consecutive already-known listings", flush=True)
            break

    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.0f}s: {len(known)} total known URLs", flush=True)
    return known
