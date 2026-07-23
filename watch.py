"""Watch loop + crawl orchestrator.

The category-table `watch_loop` iterates: each entry names a scan function
(scan_page1_*), a filter, and a deal-detection callable, so adding a new
source is one dict literal. run_used_crawl seeds those tables at startup by
running the same sources at whichever depth tier the CLI picked
(full / crawl / watch).

The per-source adapters (Vendora/Vinted/Facebook) live in crawlers/*.py and
are imported below; the shared per-listing pipeline (deal check + AI verify
+ Discord alert) lives in pipeline.py.

The stall watchdog is a last-resort safety net: sync Playwright calls have
no timeout and can wedge a scan forever, so a separate thread notices the
loop has stopped heartbeating and execv()s the process."""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime

import applog

from config import (
    RAM_URL, GPU_URL, CPU_URL, MOBO_URL,
    RAM_LOG, GPU_LOG, CPU_LOG, MOBO_LOG,
    INSOMNIA_GPU_URL, INSOMNIA_RAM_URL,
    VENDORA_GPU_LOG,
    VINTED_GPU_LOG, VINTED_CPU_LOG, VINTED_RAM_LOG, VINTED_MOBO_LOG,
    FB_GPU_LOG,
    GPU_RETAIL_URL, GPU_RETAIL_LOG,
    RAM_RETAIL_URL, RAM_RETAIL_LOG,
    CPU_RETAIL_URL, CPU_RETAIL_LOG,
    MOBO_RETAIL_URL, MOBO_RETAIL_LOG,
    LAPTOP_RETAIL_URL, LAPTOP_RETAIL_LOG,
    SCAN_INTERVAL, RETAIL_SCAN_INTERVAL)
from cleaning import clean_listings
from deals import match_gpu, is_real_gpu_card, is_gpu_deal
from crawl_utils import load_known_prices, new_unique, log_listings, recreate_page
from crawlers.skroutz import initial_crawl, scan_page1_skroutz
from crawlers.insomnia import initial_crawl_insomnia, scan_page1_insomnia
from crawlers.vendora import (                                      # noqa: F401
    _vendora_page_url, scan_page1_vendora_gpu, _initial_crawl_vendora_gpu)
from crawlers.vinted import (                                       # noqa: F401
    vinted_enabled, scan_page1_vinted, _initial_crawl_vinted)
from crawlers.facebook import (                                     # noqa: F401
    _initial_crawl_facebook_gpu, _facebook_watch_worker)
from pipeline import _process_new_listing                           # noqa: F401
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

def watch_loop(bpage, ctx, ram_known: dict, gpu_known: dict,
               cpu_known: dict | None = None, mobo_known: dict | None = None,
               do_ram: bool = True, do_gpu: bool = True,
               do_cpu: bool = False, do_mobo: bool = False,
               do_laptop: bool = False,
               do_vendora_gpu: bool = False,
               vendora_gpu_known: dict | None = None,
               do_facebook: bool = False,
               facebook_known: dict | None = None,
               do_vinted: bool = False,
               vinted_known: dict | None = None,
               do_retail: bool = False,
               ai_client=None) -> None:
    notified: set[str] = set()
    verified: set[str] = set()          # URLs already AI-verified (avoid re-paying)
    consecutive_crashes = 0
    cpu_known  = cpu_known  if cpu_known  is not None else {}
    mobo_known = mobo_known if mobo_known is not None else {}
    vendora_gpu_known = vendora_gpu_known if vendora_gpu_known is not None else {}
    facebook_known = facebook_known if facebook_known is not None else {}
    vinted_known = vinted_known if vinted_known is not None else {}
    _vk = lambda p: vinted_known.setdefault(p, {})  # per-part known {url: price}
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
                listings = cat["scan_fn"](bpage, cat["url"], log_file=cat["log_file"])
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
                cat["known"][item["url"]] = item.get("price")

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
    thread instead (see _facebook_watch_worker). Returns {url: last_price} dicts used to
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
    known = {"ram": {}, "gpu": {}, "cpu": {}, "mobo": {},
             "vendora_gpu": {}, "facebook": {},
             "vinted_gpu": {}, "vinted_cpu": {},
             "vinted_ram": {}, "vinted_mobo": {}}

    if "ram" in parts:
        k = load_known_prices(RAM_LOG)
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
        k = load_known_prices(GPU_LOG)
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
            k = load_known_prices(CPU_LOG)
            known["cpu"] = initial_crawl(bpage, k, CPU_URL, CPU_LOG, "Skroutz CPU", kind="cpu",
                                         max_pages=max_pages, early_stop_after=early)
        if want("vinted"):
            known["vinted_cpu"] = _initial_crawl_vinted(
                "cpu", VINTED_CPU_LOG, max_pages=max_pages, early_stop_after=early)

    if "mobo" in parts:
        if want("skoop"):
            k = load_known_prices(MOBO_LOG)
            known["mobo"] = initial_crawl(bpage, k, MOBO_URL, MOBO_LOG, "Skroutz Motherboard",
                                          kind="mobo", max_pages=max_pages, early_stop_after=early)
        if want("vinted"):
            known["vinted_mobo"] = _initial_crawl_vinted(
                "mobo", VINTED_MOBO_LOG, max_pages=max_pages, early_stop_after=early)

    return known
