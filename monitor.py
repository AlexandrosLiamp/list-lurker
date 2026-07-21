"""
Skroutz Skoop Monitor — RAM + GPU
──────────────────────────────────
Phase 1  Initial crawl of all pages (RAM + GPU). Skips URLs already in CSV.
Phase 2  Watch loop: scans page 1 of each category every 60 s and alerts on
         new deals via Discord webhook.

RAM deals  : DDR4/DDR5, desktop, ≥16 GB, ≥3000 MHz, below price thresholds.
GPU deals  : recognised model matched from GPU_MODELS, PPR ≥ GPU_PPR_THRESHOLD.
             PPR = performance_score / price_euros  (higher = better value).
"""

import csv
import os
import re
import shutil
import sys

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# Shared crawl helpers (page-hang guard, CSV bookkeeping, browser recovery).
from crawl_utils import (PageTimeoutException, page_timeout,           # noqa: F401
                         load_known_urls, new_unique, _known_streak_checker,
                         log_listings, prune_urls, recreate_page)

import ai_verify   # AI deal verification (degrades to no-op without anthropic SDK / API key)
import applog       # centralised logging (file + screen + tracebacks) — see applog.py

sys.stdout.reconfigure(encoding="utf-8")

# Shared logger. Configured by applog.install() in main(); using it before that is
# harmless (messages simply have nowhere to go yet).
log = applog.get_logger()

# ── Silence benign Playwright route-teardown noise ────────────────────────────
# When we navigate to the next page/query, Playwright cancels the in-flight route
# handlers for the old page. The asyncio loop then logs the interruption as an
# "Exception in callback …" — a CancelledError (handler awaited then cancelled)
# or a TargetClosedError (deferred continue_()/abort() hit a closed target). Both
# fire *after* our handler returned, so a handler-level try/except can't catch
# them, and both are harmless: the crawl has already moved on. Drop just these
# from the asyncio logger; real navigation errors are raised in the main thread
# and handled there, so they still surface normally.
import logging


class _DropRouteTeardownNoise(logging.Filter):
    _BENIGN = {"CancelledError", "TargetClosedError"}

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (exc is not None and type(exc).__name__ in self._BENIGN)


logging.getLogger("asyncio").addFilter(_DropRouteTeardownNoise())

# URLs / log paths / thresholds / keyword lists / VINTED_MODE live in config.py.
from config import *  # noqa: F401,F403  (re-exported: many external references)

# RAM parsing + sanity heuristics live in ram_specs.py (kit-aware capacities,
# speeds validated against pc-part-dataset ground truth — one source of truth).
from ram_specs import (SODIMM_KW, OLD_GEN_KW, MIN_SPEED,          # noqa: F401
                       max_capacity_gb, parse_speed_mhz, is_desktop_ddr45)

# Listing-cleaning filters (is_broken / is_clean / clean_listings) live in cleaning.py.
from cleaning import is_broken, is_clean, clean_listings  # noqa: F401  (re-exported)

# GPU performance scores live in gpu_perf.py (shared with negotiator.py — one source of truth).
from gpu_perf import GPU_MODELS, match_gpu, _GPU_RAW  # noqa: E402,F401  (_GPU_RAW kept for back-compat)

# Discord webhook + send_discord live in alerts.py.
from alerts import DISCORD_WEBHOOK, send_discord  # noqa: F401  (re-exported)


# ── Deal helpers ──────────────────────────────────────────────────────────────

from prices import parse_price, csv_price  # noqa: E402,F401  (re-export: tests import via monitor)


# Deal detection (RAM per-capacity thresholds, GPU Layer-1 classifier + PPR
# check) lives in deals.py. Layer-2 (AI verification) is applied by the watch
# loop, not by is_gpu_deal, so it stays in monitor.
from deals import (is_ram_deal, is_gpu_deal,                     # noqa: F401
                   classify_gpu_listing, is_real_gpu_card)


# Skroutz Skoop scraping (nav helpers, page extractor, initial crawl, sold
# verification) lives in crawlers/skroutz.py.
from crawlers.skroutz import (                                           # noqa: F401
    wait_for_cards, get_card_hrefs, get_total_pages, js_navigate_next,
    extract_listings, scan_page1_skroutz, initial_crawl,
    is_sold, verify_sold)


# Insomnia.gr scraping (scroll-load, extract, goto with CF-challenge handling,
# initial crawl with crash recovery, page-1 scan, and wanted-ad purge) lives
# in crawlers/insomnia.py.
from crawlers.insomnia import (                                          # noqa: F401
    _insomnia_scroll_load, extract_insomnia_listings,
    insomnia_total_pages, insomnia_page_url,
    _insomnia_is_challenge, _insomnia_goto,
    initial_crawl_insomnia, scan_page1_insomnia,
    _card_is_wanted, collect_wanted_insomnia, purge_wanted)


def purge_data() -> None:
    """Interactively delete chosen CSV 'databases' and ALL backup snapshots.
    Pure file work — no browser needed (dispatched early, like dedup_csvs).
    Asks which file(s) to wipe, then asks for confirmation before deleting.
    Per the user's choice, any successful purge also removes every backup_csv_*
    directory, regardless of which files were selected.

    Login sessions & credentials are NEVER purgeable — not by number, not by "ALL".
    These must be managed separately:
      - fb_state.json       → `python fb_marketplace.py --login`
      - skroutz_state.json  → `python recon_skroutz_offer.py --login`
      - email_config.json   → copy from email_config.example.json & fill in"""
    import glob, shutil

    # ── deny-list: files purge must NEVER touch ──────────────────────────
    _PURGE_NEVER = {"fb_state.json", "skroutz_state.json", "email_config.json"}

    # CSV "databases" are throwaway: a crawl rebuilds them.
    csv_targets = [GPU_LOG, RAM_LOG, CPU_LOG, MOBO_LOG,
                   VENDORA_GPU_LOG, FB_GPU_LOG,
                   VINTED_GPU_LOG, VINTED_CPU_LOG, VINTED_RAM_LOG, VINTED_MOBO_LOG,
                   GPU_RETAIL_LOG, RAM_RETAIL_LOG, CPU_RETAIL_LOG, MOBO_RETAIL_LOG]
    SESSION_FILE = "fb_state.json"
    targets = csv_targets + [SESSION_FILE]   # selectable by number, but not swept up by "ALL"

    print("Which database(s) to purge?\n")
    for i, f in enumerate(targets, 1):
        tag = "" if os.path.isfile(f) else "  (missing)"
        note = ("   ← FB login session, NOT auto-rebuilt (needs `fb_marketplace.py --login`)"
                if f == SESSION_FILE else "")
        print(f"  {i:2}) {f}{tag}{note}")
    print("   a) ALL databases (CSVs only — NOT login sessions or credentials)")
    sel = input("\n> ").strip().lower()

    if sel in ("a", "all"):
        chosen = list(csv_targets)           # bulk purge = throwaway CSVs only, never sessions/creds
    else:
        chosen = []
        for tok in re.split(r"[,\s]+", sel):
            if not tok:
                continue
            if tok.isdigit() and 1 <= int(tok) <= len(targets):
                f = targets[int(tok) - 1]
                if f not in chosen:
                    chosen.append(f)
            else:
                print(f"Ignoring unrecognised selection '{tok}'.")
    if not chosen:
        print("Nothing selected. Aborted.")
        return

    # ── hard guard: refuse to delete any deny-listed file ──────────────
    blocked = [f for f in chosen if f in _PURGE_NEVER]
    if blocked:
        print(f"\n⚠ REFUSED: these files are login sessions / credentials and will NOT be deleted:")
        for f in blocked:
            print(f"    {f}")
        chosen = [f for f in chosen if f not in _PURGE_NEVER]
        if not chosen:
            print("Nothing left to purge. Aborted.")
            return

    backups = sorted(glob.glob("backup_csv_*"))
    print("\nThis will PERMANENTLY DELETE:")
    for f in chosen:
        print(f"  - {f}" + ("" if os.path.isfile(f) else "  (missing)"))
    if SESSION_FILE in chosen:
        print(f"  ⚠ {SESSION_FILE} is your FB login session — deleting it logs the scraper "
              f"out; restore with `python fb_marketplace.py --login`.")
    if backups:
        print(f"  - ALL backup snapshots ({len(backups)} dir): " + ", ".join(backups))
    confirm = input('\nType "yes" to confirm: ').strip().lower()
    if confirm != "yes":
        print("Aborted. Nothing was deleted.")
        return

    for f in chosen:
        try:
            os.remove(f)
            print(f"  deleted {f}")
        except FileNotFoundError:
            print(f"  (already gone) {f}")
        except OSError as e:
            print(f"  FAILED {f}: {e}")
    for d in backups:
        shutil.rmtree(d, ignore_errors=True)
        print(f"  removed {d}/")
    log.info("purge: deleted %s + %d backup dir(s)", ", ".join(chosen), len(backups))
    print("Purge complete.")


def dedup_csvs() -> None:
    """One-shot cleanup of existing duplicate rows. Keeps one row per (url, price)
    — collapses identical repeats while preserving genuine price changes. Backs up first."""
    import shutil
    from datetime import datetime as _dt
    bdir = "backup_csv_" + _dt.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(bdir, exist_ok=True)
    before = after = 0
    for f in (RAM_LOG, GPU_LOG, CPU_LOG, MOBO_LOG):
        if not os.path.isfile(f):
            continue
        with open(f, encoding="utf-8") as fh:
            rdr = csv.DictReader(fh); fields = rdr.fieldnames; rows = list(rdr)
        shutil.copy(f, os.path.join(bdir, f))
        seen, kept = set(), []
        for r in rows:
            url = (r.get("url") or "").strip()
            if not url:
                kept.append(r); continue
            key = (url, str(r.get("price") or "").strip())
            if key in seen:
                continue
            seen.add(key); kept.append(r)
        with open(f, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(kept)
        print(f"  {f:18} {len(rows):>5} -> {len(kept):>5}  ({len(rows)-len(kept)} duplicate rows removed)")
        before += len(rows); after += len(kept)
    print(f"\nTotal: {before} -> {after} rows ({before-after} removed). Backup: {bdir}")


# Watch loop, source adapters (Vendora/Vinted/Facebook), and run_used_crawl
# live in watch.py.
from watch import (                                                       # noqa: F401
    watch_loop, run_used_crawl,
    scan_page1_vendora_gpu, scan_page1_vinted, vinted_enabled,
    EARLY_STOP_KNOWN, WATCH_SEED_PAGES)


# ── AI verification (manual / on-demand) ──────────────────────────────────────

def _print_analysis(url: str, a, prefix: str = "") -> None:
    if a is None:
        print(f"  {prefix}{url[:70]}\n    → could not verify (page/CLI failed)", flush=True)
        return
    flag = "SOLD/CLOSED" if not a.overall_available else ("MULTI-ITEM" if a.is_multi_item else "OK")
    print(f"  {prefix}{url[:70]}\n    → {flag}", flush=True)
    for it in a.items:
        avail = "available" if it.available else "SOLD"
        price = f"{it.price:.0f}€" if it.price is not None else "?"
        print(f"        - [{avail}] {price:>6}  {it.name[:55]}", flush=True)
    if a.notes:
        print(f"        notes: {a.notes[:120]}", flush=True)


def _deal_candidates(log_file: str, deal_fn) -> list[tuple[str, str]]:
    """Unique (url, name) from a CSV whose rows currently qualify as a deal."""
    if not os.path.isfile(log_file):
        return []
    seen, out = set(), []
    with open(log_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()
            if not url or url in seen:
                continue
            item = {"name": row.get("name", ""), "condition": row.get("condition", ""),
                    "url": url, "price": csv_price(row.get("price"))}
            if deal_fn(item):
                seen.add(url)
                out.append((url, item["name"]))
    return out


def run_ai_verify(bpage, target: str, limit: int = 30) -> None:
    """Manual AI verification. `target` is a listing URL, or one of ram|gpu|all."""
    ai_client = ai_verify.get_client()
    if not ai_client:
        print("Claude CLI not found on PATH. Install Claude Code and run `claude login`, then retry.")
        return

    if target.startswith("http"):
        print(f"\n── AI VERIFY (single listing) ──")
        a = ai_verify.verify_listing(bpage, target, "(manual)", "unknown", ai_client)
        _print_analysis(target, a)
        return

    target = target.lower()
    jobs = []
    if target in ("ram", "all"):
        jobs.append(("ram", RAM_LOG, lambda it: is_ram_deal(it)))
    if target in ("gpu", "all"):
        jobs.append(("gpu", GPU_LOG, lambda it: (is_gpu_deal(it) or (None, None))[0]))
    if not jobs:
        print(f"aiverify target must be a listing URL or one of: ram, gpu, all  (got {target!r})")
        return

    for kind, log_file, deal_fn in jobs:
        cands = _deal_candidates(log_file, deal_fn)
        capped = cands[:limit]
        print(f"\n── AI VERIFY {kind.upper()} ── {len(cands)} deal candidate(s)"
              + (f", checking first {limit}" if len(cands) > limit else ""))
        sold = set()
        for i, (url, name) in enumerate(capped, 1):
            try:
                a = ai_verify.verify_listing(bpage, url, name, kind, ai_client)
            except Exception as e:
                a = None
                print(f"  [{i}/{len(capped)}] error: {str(e)[:80]}", flush=True)
            _print_analysis(url, a, prefix=f"[{i}/{len(capped)}] ")
            if a is not None and not a.overall_available:
                sold.add(url)
        if sold:
            n = prune_urls(log, sold)
            print(f"  → pruned {n} sold/closed row(s) from {log}")
    print("\nAI verification complete.")


# Skroutz retail (main site, not skoop) scraping + snapshot/drop detection +
# run_retail_crawl lives in retail.py.
from retail import (extract_retail_listings, retail_next_page_url,       # noqa: F401
                    crawl_retail, crawl_retail_gpus, log_retail,
                    save_retail_snapshot, detect_retail_drops,
                    write_retail_deals, log_retail_laptops, run_retail_crawl)


def _block_heavy(route):
    """Abort image/font/media/stylesheet requests to speed up page loads.
    We only read the DOM, so these resources are pure overhead."""
    try:
        if route.request.resource_type in ("image", "media", "font", "stylesheet"):
            route.abort()
        else:
            route.continue_()
    except Exception:
        try: route.continue_()
        except Exception: pass


# ── Entry point ───────────────────────────────────────────────────────────────
# Three crawl-depth tiers share one driver:
#   crawl full   → every page of all used sources, plus the Skroutz retail
#                  catalogs (also full). One-shot.
#   crawl        → stop a source after EARLY_STOP_KNOWN consecutive already-known
#                  listings (feeds are newest-first). No retail. One-shot.
#   watch        → crawl the first WATCH_SEED_PAGES pages, then watch page 1 for
#                  new listings + Discord alerts. No retail.
# Retail (skroutz.gr, not skoop) is always a FULL crawl and only runs via
# `crawl full` or the manual `crawl skroutz`.


ALL_PARTS = ("ram", "gpu", "cpu", "mobo", "laptop")
SOURCE_TOKENS = {"skoop", "insomnia", "vendora", "facebook", "vinted"}


def _parse_parts(token):
    """Resolve a part token into a set of parts. None/'all'/'parts' → everything;
    an unknown token → empty set (caller shows usage)."""
    if token in (None, "", "all", "parts"):
        return set(ALL_PARTS)
    if token in ALL_PARTS:
        return {token}
    return set()


def print_usage():
    print("Usage: python monitor.py <command> [source] [part]\n")
    print("Source and part are both optional and order-independent (a token is")
    print("recognised as a source or a part automatically).")
    print()
    print("Crawl tiers:")
    print("  crawl full  [source] [part]  Crawl EVERY page. One-shot. Scope it:")
    print("                                 crawl full            every part, every source, + retail")
    print("                                 crawl full gpu        GPU from every source, + retail")
    print("                                 crawl full vinted     every part from Vinted only")
    print("                                 crawl full vinted gpu GPU from Vinted only")
    print("                               (retail runs only when no source is named.)")
    print("  crawl       [source] [part]  Crawl until 10 consecutive already-known listings,")
    print("                               then stop (Facebook stops at known too). No retail.")
    print("                                 crawl gpu             GPU from every source")
    print("                                 crawl vinted          every part from Vinted")
    print("                                 crawl vinted gpu      GPU from Vinted")
    print("  watch       [part]           Seed a few pages, then watch + Discord alerts. No retail.")
    print("  crawl skroutz [part]         Skroutz RETAIL catalog only (always full).")
    print()
    print("  source = skoop | insomnia | vendora | facebook | vinted")
    print("  part   = ram | gpu | cpu | mobo | laptop | all      (omit = all)")
    print()
    print("Maintenance:")
    print("  verify                       Visit each skoop listing, prune sold ones (one-shot)")
    print("  aiverify [<url>|ram|gpu|all] Claude-verify a listing or the deal candidates")
    print("  purgewanted                  Prune insomnia Ζήτηση (wanted) ads from the CSVs")
    print("  dedup                        Collapse duplicate CSV rows (no browser)")
    print("  purge                        Interactively delete CSV databases + all backups")
    print()
    print("Examples:")
    print("  python monitor.py crawl full          full crawl of everything + retail")
    print("  python monitor.py crawl full vinted   full crawl of every Vinted part")
    print("  python monitor.py crawl gpu           quick GPU crawl, every source (early-stop)")
    print("  python monitor.py crawl facebook gpu  GPU, Facebook only (stops at known)")
    print("  python monitor.py watch               seed, then watch all parts")
    print("  python monitor.py crawl skroutz gpu   GPU retail catalog only")


def _classify_tokens(tokens):
    """Split crawl tokens into (sources_set, part_token, bad_token). Each token is
    either a source (skoop/insomnia/…) or a part (gpu/ram/…/all); order does not
    matter. Returns bad_token (the first unrecognised one) when something doesn't
    fit so the caller can show usage."""
    srcs: set[str] = set()
    part_tok = None
    for tok in tokens:
        if not tok:
            continue
        if tok in SOURCE_TOKENS:
            srcs.add(tok)
        elif tok in ALL_PARTS or tok in ("all", "parts"):
            part_tok = tok
        else:
            return None, None, tok
    return srcs, part_tok, None


def main():
    applog.install("scoop")   # set up file logging + crash capture (call once, first)
    raw = sys.argv[1:]
    log.info("monitor.py started: %s", " ".join(raw) if raw else "(watch)")
    verb = raw[0].lower() if raw else "watch"     # bare `monitor.py` → watch all parts
    arg1 = raw[1] if len(raw) > 1 else None
    arg2 = raw[2] if len(raw) > 2 else None
    arg3 = raw[3] if len(raw) > 3 else None
    t1 = arg1.lower() if arg1 else None
    t2 = arg2.lower() if arg2 else None
    t3 = arg3.lower() if arg3 else None

    if verb in ("help", "-h", "--help"):
        print_usage(); return

    # ── Dedup is pure CSV work — no browser needed ──
    if verb == "dedup":
        print("Deduplicating CSVs (keeping one row per URL + price)…")
        dedup_csvs(); return

    # ── Purge is pure file work — no browser needed ──
    if verb == "purge":
        purge_data(); return

    # ── Resolve the command into an action plan ──
    # action ∈ {verify, aiverify, purgewanted, retail, oneshot, watch}
    action = depth = sources = None
    parts = set(ALL_PARTS)

    if verb in ("verify", "aiverify", "purgewanted"):
        action = verb
    elif verb == "watch":
        action, depth = "watch", "watch"
        parts = _parse_parts(t1)
    elif verb == "crawl":
        if t1 in ("skroutz", "skrootz", "retail"):
            action, parts = "retail", _parse_parts(t2)
        elif t1 == "full":
            # crawl full [source] [part]  — source and/or part, any order, both optional
            action, depth = "oneshot", "full"
            srcs, part_tok, bad = _classify_tokens([t2, t3])
            if bad is not None:
                print(f"Unknown source/part '{bad}'.\n"); print_usage(); sys.exit(1)
            sources = srcs or None
            parts = _parse_parts(part_tok)
        else:
            # crawl [source] [part]  — source and/or part, any order, both optional
            action, depth = "oneshot", "crawl"
            srcs, part_tok, bad = _classify_tokens([t1, t2])
            if bad is not None:
                print(f"Unknown source/part '{bad}'.\n"); print_usage(); sys.exit(1)
            sources = srcs or None
            parts = _parse_parts(part_tok)
    else:
        print(f"Unknown command '{verb}'.\n"); print_usage(); sys.exit(1)

    if not parts:
        print(f"Unknown part '{arg1}'.\n")
        print_usage(); sys.exit(1)

    # ── Banner ──
    ai_client = ai_verify.get_client()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║  Skroutz Skoop + Insomnia + Vendora + Facebook + Vinted  PC monitor   ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print(f"Command   : {' '.join(raw) if raw else 'watch'}")
    if action in ("oneshot", "watch", "retail"):
        tier = {"full": "FULL CRAWL", "crawl": "CRAWL (early-stop)",
                "watch": "WATCH"}.get(depth, "RETAIL (full)")
        print(f"Tier      : {tier}")
        print(f"Parts     : {', '.join(p for p in ALL_PARTS if p in parts)}")
        if sources:
            print(f"Sources   : {', '.join(sorted(sources))}")
    print(f"Discord   : {'✓ webhook configured' if DISCORD_WEBHOOK else '✗ not set'}")
    print(f"AI verify : {'✓ Claude CLI (' + ai_verify.MODEL + ')' if ai_client else '✗ off (claude CLI not on PATH)'}")
    print("\nPress Ctrl+C to stop.\n")

    with Stealth().use_sync(sync_playwright()) as pw:
        # Quiet-fingerprint launch flags for the CF-challenged sources (Skoop/Insomnia),
        # borrowed from Crawl4AI's stealth config (github.com/unclecode/crawl4ai,
        # Apache-2.0). The FB worker deliberately keeps its long-lived fingerprint —
        # changing it under a saved login session invites a re-verification.
        browser = pw.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check", "--disable-infobars",
            "--force-color-profile=srgb", "--mute-audio",
            "--disable-background-networking", "--disable-component-update",
            "--disable-domain-reliability",
            "--disable-features=OptimizationHints,MediaRouter,DialMediaRouteProvider,TranslateUI",
        ])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"),
            locale="el-GR",
            viewport={"width": 1280, "height": 900},
        )
        # ── Speedup: don't download images/fonts/media/css — we only read the DOM ──
        ctx.route("**/*", _block_heavy)
        bpage = ctx.new_page()

        if action == "verify":
            verify_sold(bpage, [RAM_LOG, GPU_LOG, CPU_LOG, MOBO_LOG])
            return

        if action == "aiverify":
            run_ai_verify(bpage, arg1 or "all")
            return

        if action == "purgewanted":
            purge_wanted(bpage, ctx)
            return

        if action == "retail":
            run_retail_crawl(bpage, parts)
            print("\nRetail crawl complete.")
            return

        if action == "oneshot":
            # The full tier includes a full retail crawl — but only when no single
            # source was requested (retail is Skroutz-specific; `crawl full vinted`
            # shouldn't drag in retail).
            if depth == "full" and sources is None:
                run_retail_crawl(bpage, parts)
            run_used_crawl(bpage, ctx, parts, depth, sources=sources)
            print(f"\n{'Full crawl' if depth == 'full' else 'Crawl'} complete.")
            return

        # action == "watch": seed the fast sources, then watch. Facebook is skipped here
        # and seeded by its own background thread so it doesn't block startup; its known
        # set is loaded straight from the CSV.
        known = run_used_crawl(bpage, ctx, parts, "watch", skip_facebook=True)
        facebook_known = load_known_urls(FB_GPU_LOG) if "gpu" in parts else set()
        watch_loop(bpage, ctx, known["ram"], known["gpu"],
                   cpu_known=known["cpu"], mobo_known=known["mobo"],
                   do_ram=("ram" in parts), do_gpu=("gpu" in parts),
                   do_cpu=("cpu" in parts), do_mobo=("mobo" in parts),
                   do_laptop=("laptop" in parts),
                   do_vendora_gpu=("gpu" in parts),
                   vendora_gpu_known=known["vendora_gpu"],
                   do_facebook=("gpu" in parts),
                   facebook_known=facebook_known,
                   do_vinted=True,
                   vinted_known={"gpu": known["vinted_gpu"], "cpu": known["vinted_cpu"],
                                 "ram": known["vinted_ram"], "mobo": known["vinted_mobo"]},
                   do_retail=False, ai_client=ai_client)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
