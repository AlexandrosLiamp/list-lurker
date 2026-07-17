"""
Facebook Marketplace GPU listing crawler
─────────────────────────────────────────
Scrapes Facebook Marketplace for GPU listings with a HEADLESS browser (Playwright).
Results are sorted newest-first and the page is SCROLLED to lazy-load more cards:

  • full  mode → scroll each search query to the bottom (initial / one-shot crawl).
  • watch mode → scroll only until we reach already-known listings, then move on
                 (newest-first ordering means the fresh listings are at the top).

Login: Facebook heavily throttles logged-out guests. Run `--login` ONCE to open a
visible browser, sign in by hand (clearing any checkpoint/2FA), and the session is
saved to fb_state.json. Every later crawl reuses that session silently. No password
is stored in this file.

Why a browser and not plain HTTP? Marketplace listings load via authenticated
GraphQL — a `requests`/`curl` fetch returns only a login-wall shell. The headless
browser runs invisibly (no window) once a session exists.

Output schema (fb_gpu.csv) mirrors the skoop/insomnia GPU data plus a model+score:
    timestamp, name, condition, price, model, score, url

The extraction core (`crawl_facebook_gpu`) is reused by monitor.py so there is a
single implementation.

Usage:
    python fb_marketplace.py --login            # one-time: sign in, save session
    python fb_marketplace.py                     # full multi-query crawl (scrolls)
    python fb_marketplace.py --watch             # watch-mode crawl (stops at known)
    python fb_marketplace.py --max-items 200     # stop after 200 GPU listings
    python fb_marketplace.py --dry-run           # preview without saving CSV
"""

import csv
import os
import random
import re
import sys
import time
from datetime import datetime

from gpu_perf import match_gpu
from listing_common import _norm, WANTED_KW, TRADE_KW, is_wanted_or_trade  # noqa: F401 (re-exported)

sys.stdout.reconfigure(encoding="utf-8")

# ── Config ────────────────────────────────────────────────────────────────────
FB_LOCATION = "106070886100190"     # Patra marketplace ID
FB_RADIUS   = 250                   # km (max for the area)
FB_DAYS     = 7                     # only listings from the last N days
FB_SORT     = "creation_time_descend"  # newest-first → enables scroll-until-known

# Saved login session (cookies + localStorage). Created by `--login`, reused after.
STATE_FILE = "fb_state.json"

# Scroll tuning
SCROLL_PAUSE         = 2.0   # seconds to wait after each scroll for lazy-load
SETTLE               = 4.0   # seconds after initial page load before first read
MAX_SCROLLS_FULL     = 100   # hard safety cap for a full crawl (per query)
MAX_SCROLLS_WATCH    = 25    # hard safety cap while watching (per query)
WATCH_BARREN_BATCHES = 2     # stop after N consecutive all-known scroll batches
IDLE_SCROLLS_STOP    = 2     # stop after N scrolls that load zero new cards (bottom)
QUERY_DELAY_RANGE    = (4.0, 9.0)  # randomized pause between queries (anti-throttle)

# Multiple search queries to cover different GPU naming patterns. Only the query=
# part of the URL changes between them; days/sort/radius stay fixed (build_fb_url).
SEARCH_QUERIES = [
    "κάρτα γραφικών",              # Greek: graphics card
    "καρτα γραφικων",              # Greek: graphics card (no accent)
    "karta grafikon",              # Greeklish
    "geforce",                     # NVIDIA
    "radeon",                      # AMD
    "nvidia",                      # NVIDIA brand
    "rtx",                         # RTX series
    "gtx",                         # GTX series
    "rx 5",                        # RX 5000 series
    "rx 6",                        # RX 6000 series
    "rx 7",                        # RX 7000 series
    "rx 9",                        # RX 9000 series
    "arc",                         # Intel Arc
    "vga",                         # common Greek title for a graphics card
    "gpu",                         # generic
]

LOG_FILE = "fb_gpu.csv"
MAX_GPU_ITEMS = 500
NAV_TIMEOUT = 60000

# GPU model database + match_gpu are imported from gpu_perf (single source of truth).

GPU_KEYWORDS = [
    "κάρτα γραφικών", "καρτα γραφικων",
    "karta grafikon", "karta grafikwn",
    "gpu", "graphics card", "video card", "vga",
]
GPU_BRAND_KW = [
    "geforce", "radeon", "nvidia", "amd", "intel arc",
    "rtx", "gtx", "rx ", "quadro", "tesla", "firepro",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def canonical_fb_url(url: str) -> str:
    """Reduce a Marketplace listing URL to a stable, dedup-safe form.

    Facebook appends a per-search `tracking=browse_serp:<uuid>` (plus ref/referral
    params) that is UNIQUE to every query and scroll batch, so the same listing
    yields a different full URL on every read. Keying dedup on the full URL therefore
    fails and the same card gets logged dozens of times. Collapsing to
    `https://www.facebook.com/marketplace/item/<id>/` gives one stable key per item.
    Non-marketplace URLs are returned unchanged."""
    if not url:
        return url
    m = re.search(r"/marketplace/item/(\d+)", url)
    return f"https://www.facebook.com/marketplace/item/{m.group(1)}/" if m else url


def parse_price(text: str) -> float | None:
    """Parse a price from FB price text, handling European formatting:
    '220 €' → 220.0, '1.250 €' → 1250.0, '1.299,99 €' → 1299.99, '€ 350' → 350.0."""
    if not text:
        return None
    t = str(text).replace("\xa0", " ").strip()
    m = (re.search(r"(\d[\d.,]*)\s*€", t)      # number before the €
         or re.search(r"€\s*(\d[\d.,]*)", t)   # number after the €
         or re.search(r"\d[\d.,]*", t))        # bare number (FB usually shows €)
    if not m:
        return None
    num = (m.group(1) if m.lastindex else m.group(0)).strip().strip(".,")
    if "," in num and "." in num:
        # both separators present: the right-most one is the decimal point
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        # comma alone is decimal only when it groups 1-2 trailing digits (e.g. 12,50)
        num = (num.replace(",", ".")
               if (num.count(",") == 1 and re.search(r",\d{1,2}$", num))
               else num.replace(",", ""))
    elif "." in num:
        # dot alone is a thousands separator when it groups 3 digits (1.250) or repeats
        if num.count(".") > 1 or re.search(r"\.\d{3}$", num):
            num = num.replace(".", "")
        # otherwise it's a genuine decimal point (e.g. 350.00) — leave as-is
    try:
        val = float(num)
    except ValueError:
        return None
    return val if val > 0 else None


# Condition keywords → canonical Greek label (matches the dashboard's condition
# filter and the skoop/insomnia vocabulary). FB search cards have no condition
# field, so this is a best-effort read of the title; '' when there's no signal.
def detect_condition(name: str) -> str:
    n = _norm(name)
    if ("σαν καινουρ" in n or "ελαχιστα χρησιμοπ" in n or "ελαχιστη χρηση" in n
            or "σχεδον καινουρ" in n or "αριστη κατασταση" in n or "like new" in n):
        return "Ελάχιστα χρησιμοποιημένο"
    if (("σφραγισμ" in n and "ασφραγ" not in n) or "ολοκαινουρ" in n
            or "καινουργ" in n or "καινουριο" in n or "καινουρια" in n
            or "brand new" in n or "sealed" in n):
        return "Καινούριο"
    if "μεταχειρ" in n or "second hand" in n or "second-hand" in n:
        return "Μεταχειρισμένο"
    return ""


def is_gpu_listing(name: str) -> tuple[str, int] | None:
    n = _norm(name)
    match = match_gpu(name)
    if match: return match
    has_gpu_kw = any(kw in n for kw in GPU_KEYWORDS)
    has_brand = any(kw in n for kw in GPU_BRAND_KW)
    has_model_num = bool(re.search(r"\b(rx|rtx|gtx|gt|hd|r[579])\s*\d", n))
    if has_gpu_kw and (has_brand or has_model_num):
        return ("GPU (unrecognised model)", 0)
    return None


# WANTED_KW / TRADE_KW / is_wanted_or_trade are imported from listing_common.


# ── FB URL builder ────────────────────────────────────────────────────────────

def build_fb_url(query: str) -> str:
    """Build the Facebook Marketplace search URL for a given query term. Only the
    query= value changes between searches; location, days, sort and radius are fixed."""
    from urllib.parse import quote
    encoded = quote(query)
    return (f"https://www.facebook.com/marketplace/{FB_LOCATION}/search/"
            f"?daysSinceListed={FB_DAYS}"
            f"&sortBy={FB_SORT}"
            f"&query={encoded}"
            f"&exact=false&radius={FB_RADIUS}")


# ── Listing extraction ────────────────────────────────────────────────────────

def extract_fb_listings(page) -> list[dict]:
    """Extract marketplace listings from the Facebook DOM."""
    raw = page.evaluate(r"""
        () => {
            const results = [];
            document.querySelectorAll('a[href*="/marketplace/item/"]').forEach(a => {
                const href = a.href;
                if (!href) return;

                let container = a;
                for (let i = 0; i < 5; i++) {
                    if (container.parentElement) container = container.parentElement;
                }
                const fullText = container ? container.innerText : '';
                const lines = fullText.split('\n').map(s => s.trim()).filter(Boolean);

                const aria = a.getAttribute('aria-label') || '';
                const img = a.querySelector('img');
                const alt = img ? (img.alt || '') : '';

                let title = '';
                let priceText = '';

                const priceLines = [];
                const textLines = [];

                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed) continue;
                    if (/^€/.test(trimmed) || /€$/.test(trimmed) || /^\d+[.,]?\d*$/.test(trimmed)) {
                        priceLines.push(trimmed);
                    } else {
                        textLines.push(trimmed);
                    }
                }

                for (const line of textLines) {
                    if (/^[A-Zα-ωίϊΐόάέύϋήώ].*, [A-Z]$/i.test(line) && line.length < 30) continue;
                    if (line.length > 2 && !title) {
                        title = line;
                        break;
                    }
                }

                priceText = priceLines[0] || '';

                if (!title && aria) {
                    const ariaParts = aria.split(/\s*€\s*/);
                    if (ariaParts.length >= 2) {
                        const before = ariaParts[0].trim();
                        const priceSplit = before.match(/^(.*?),\s*(\d+[.,]?\d*)$/);
                        if (priceSplit) {
                            title = priceSplit[1].trim();
                            if (!priceText) priceText = '€ ' + priceSplit[2];
                        } else {
                            title = before;
                        }
                    }
                }

                if (!title && alt) {
                    title = alt.replace(/ στην ομάδα.*$/, '').replace(/ στην ομαδα.*$/, '').trim();
                }

                const fullUrl = href.startsWith('http') ? href : 'https://www.facebook.com' + href;

                results.push({
                    url: fullUrl,
                    title: (title || '').replace(/\s+/g, ' ').trim(),
                    priceText: priceText.replace(/\s+/g, ' ').trim(),
                });
            });
            return results;
        }
    """)

    listings = []
    seen_ids = set()
    for card in raw:
        try:
            name = (card.get("title") or "").strip()[:300]
            url  = (card.get("url") or "").strip()
            price_raw = (card.get("priceText") or "").strip()
            price = parse_price(price_raw)

            item_id = url.split("/marketplace/item/")[-1].split("/")[0].split("?")[0]
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            url = canonical_fb_url(url)   # strip per-search tracking params (dedup key)
            if name and url:
                listings.append({
                    "name": name,
                    "price": price,
                    "price_raw": price_raw,
                    "url": url,
                })
        except Exception:
            continue
    return listings


# ── Logging ───────────────────────────────────────────────────────────────────

def load_existing_urls(log_file: str) -> set[str]:
    if not os.path.isfile(log_file):
        return set()
    known: set[str] = set()
    with open(log_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()   # guard against short/None rows
            if url: known.add(canonical_fb_url(url))
    print(f"  {log_file}: {len(known)} existing GPU listings")
    return known


def log_listings(listings: list[dict], log_file: str) -> None:
    file_exists = os.path.isfile(log_file)
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "name", "condition", "price", "model", "score", "url",
        ])
        if not file_exists: writer.writeheader()
        for item in listings:
            writer.writerow({
                "timestamp": item["timestamp"],
                "name": item["name"],
                "condition": item.get("condition", ""),
                "price": item["price"],
                "model": item["model"],
                "score": item["score"],
                "url": item["url"],
            })


# ── Scrolling ───────────────────────────────────────────────────────────────

def _is_blocked(page) -> bool:
    """True if Facebook bounced us to its anti-bot / login wall. After several rapid
    automated searches FB redirects Marketplace to `/marketplace/ineligible/` (or to
    a login page); continuing to hammer it only deepens the block, so the crawl stops
    and the caller backs off."""
    try:
        u = page.url or ""
    except Exception:
        return False
    return ("/marketplace/ineligible" in u
            or "/checkpoint" in u
            or u.rstrip("/").endswith("facebook.com/login")
            or "login/?" in u
            or "login.php" in u)


def _collect_with_scroll(page, known: set[str], mode: str, *,
                         scroll_pause: float, max_scrolls: int,
                         barren_limit: int) -> list[dict]:
    """Scroll a loaded search page, collecting cards as Facebook lazy-loads them.

    Returns every unique raw card seen (dicts from extract_fb_listings).

    Stop conditions:
      • Bottom reached — `IDLE_SCROLLS_STOP` scrolls in a row add no new cards.
      • Hard cap       — `max_scrolls` scrolls (safety).
      • Watch only     — `barren_limit` consecutive scroll *batches* whose newly
                         loaded cards contain no genuinely-new GPU. Because the
                         page is newest-first, once we scroll past the fresh
                         listings into already-known territory we can stop. A new
                         listing appearing in any batch resets the counter, which
                         absorbs Facebook's newest-first ordering jitter.
    """
    seen: set[str] = set()
    collected: list[dict] = []
    barren = 0   # consecutive all-known batches (watch early-stop)
    idle = 0     # consecutive scrolls that produced no new cards (bottom of list)

    for i in range(max_scrolls + 1):
        raw = extract_fb_listings(page)
        fresh = [c for c in raw if c["url"] not in seen]
        for c in fresh:
            seen.add(c["url"])
            collected.append(c)

        # Bottom-of-results detection (ignore the very first read at i == 0)
        if i > 0 and not fresh:
            idle += 1
            if idle >= IDLE_SCROLLS_STOP:
                break
        else:
            idle = 0

        # Watch early-stop: did this freshly-loaded batch hold any new GPU?
        if mode == "watch" and fresh:
            batch_has_new_gpu = any(
                c["url"] not in known
                and is_gpu_listing(c["name"])
                and not is_wanted_or_trade(c["name"])
                for c in fresh
            )
            barren = 0 if batch_has_new_gpu else barren + 1
            if barren >= barren_limit:
                break

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(scroll_pause)

    return collected


# ── Browser / session ─────────────────────────────────────────────────────────

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.0.0 Safari/537.36")


def _block_heavy(route):
    """Drop images/media/fonts to speed scrolling. Keep CSS + JS — FB needs them
    to render and lazy-load the listing cards."""
    try:
        if route.request.resource_type in ("image", "media", "font"):
            route.abort()
        else:
            route.continue_()
    except Exception:
        try: route.continue_()
        except Exception: pass


def make_fb_context(browser, *, block_heavy: bool = True):
    """Create a browser context seeded with the saved FB login session (if any).
    Reused by both the standalone crawler and monitor.py so the session/UA/locale
    setup lives in one place."""
    state = STATE_FILE if os.path.isfile(STATE_FILE) else None
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        locale="el-GR",
        viewport={"width": 1366, "height": 900},
        storage_state=state,
    )
    if block_heavy:
        ctx.route("**/*", _block_heavy)
    return ctx


def login(headful: bool = True) -> bool:
    """One-time interactive login. Opens a VISIBLE browser; you sign in (and clear
    any checkpoint/2FA), then the session is saved to STATE_FILE for reuse.

    If FB_EMAIL / FB_PASSWORD env vars are set the form is pre-filled to save typing,
    but you can always just log in by hand in the window. No password is stored."""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    email = os.environ.get("FB_EMAIL", "")
    password = os.environ.get("FB_PASSWORD", "")

    print("Opening a browser window for Facebook login…")
    print("Sign in (clear any checkpoint/2FA). The session saves automatically once")
    print("you're logged in. Window stays open up to 5 minutes.\n")

    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(
            headless=not headful,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT, locale="el-GR",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        try:
            page.goto("https://www.facebook.com/login",
                      wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception as e:
            print(f"✗ Could not open Facebook: {str(e)[:120]}")
            ctx.close(); browser.close()
            return False

        # Best-effort pre-fill if creds were provided via env vars.
        if email and password:
            try:
                page.fill("input[name='email']", email, timeout=8000)
                page.fill("input[name='pass']", password, timeout=8000)
                page.click("button[name='login']", timeout=8000)
            except Exception:
                pass  # fall back to manual login in the window

        # FB sets the 'c_user' cookie once authenticated — poll for it.
        print("Waiting for login to complete…")
        deadline = time.time() + 300
        logged_in = False
        while time.time() < deadline:
            try:
                if any(c.get("name") == "c_user" for c in ctx.cookies()):
                    logged_in = True
                    break
            except Exception:
                pass
            time.sleep(2)

        if not logged_in:
            print("✗ Timed out waiting for login — nothing saved.")
            ctx.close(); browser.close()
            return False

        ctx.storage_state(path=STATE_FILE)
        print(f"✅ Login saved to {os.path.abspath(STATE_FILE)}")
        ctx.close(); browser.close()
        return True


# ── Main ──────────────────────────────────────────────────────────────────────

def crawl_facebook_gpu(page, known: set[str] | None = None, ts: str | None = None,
                       mode: str = "full", max_items: int = MAX_GPU_ITEMS,
                       settle: float = SETTLE, scroll_pause: float = SCROLL_PAUSE,
                       nav_timeout: int = NAV_TIMEOUT, verbose: bool = True,
                       stats: dict | None = None) -> list[dict]:
    """Run the multi-query FB Marketplace GPU scan on an EXISTING (ideally logged-in)
    Playwright page, scrolling each query to load more results.

    mode="full"  → scroll each query to the bottom (initial / one-shot crawl).
    mode="watch" → scroll only until already-known listings appear, then move on.

    Returns the list of NEW GPU listing dicts — the caller decides whether/where to
    save them. `known` (a set of already-seen listing URLs) is updated in place so
    a caller can reuse it across runs.

    If `stats` (a dict) is given it is filled with run diagnostics, notably
    stats['blocked'] = True when Facebook redirected us to its anti-bot wall. The
    caller (monitor.py) uses that to back off instead of hammering the block.

    Shared by the standalone crawler below and by monitor.py's pipeline, so the
    extraction logic lives in exactly one place."""
    known = set() if known is None else known
    # Normalize any caller-supplied known URLs (e.g. monitor.py loads raw URLs that
    # may still carry old tracking params) so they match the canonical extracted ones.
    dirty = {u for u in known if u != canonical_fb_url(u)}
    if dirty:
        known.difference_update(dirty)
        known.update(canonical_fb_url(u) for u in dirty)
    ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_items: list[dict] = []
    seen_this_run: set[str] = set()
    total_raw = 0
    blocked = False
    queries_run = 0
    max_scrolls = MAX_SCROLLS_WATCH if mode == "watch" else MAX_SCROLLS_FULL

    for qi, query in enumerate(SEARCH_QUERIES):
        if max_items and len(new_items) >= max_items:
            if verbose:
                print(f"  Reached max_items limit ({max_items}).", flush=True)
            break

        try:
            page.goto(build_fb_url(query), wait_until="domcontentloaded", timeout=nav_timeout)
        except Exception:
            if verbose:
                print(f"  [{qi+1}/{len(SEARCH_QUERIES)}] '{query}': timeout — skip", flush=True)
            continue

        time.sleep(settle)

        # Anti-bot wall? Stop immediately — more requests only deepen the block.
        if _is_blocked(page):
            blocked = True
            if verbose:
                print(f"  [{qi+1}/{len(SEARCH_QUERIES)}] '{query}': Facebook blocked this "
                      f"session (marketplace/ineligible or login wall) — stopping the "
                      f"crawl to back off.", flush=True)
            break

        queries_run += 1
        raw = _collect_with_scroll(
            page, known, mode,
            scroll_pause=scroll_pause, max_scrolls=max_scrolls,
            barren_limit=WATCH_BARREN_BATCHES)
        total_raw += len(raw)
        query_gpu = 0

        for item in raw:
            u = (item.get("url") or "").strip()
            n = (item.get("name") or "").strip()
            if not u or not n or u in seen_this_run:
                continue
            seen_this_run.add(u)

            match = is_gpu_listing(n)
            if not match or is_wanted_or_trade(n) or u in known:
                continue

            known.add(u)
            model_name, score = match
            new_items.append({
                "timestamp": ts,
                "name": n,
                "condition": detect_condition(n),
                "price": item.get("price"),            # already parsed by extract_fb_listings
                "price_raw": item.get("price_raw", ""),
                "model": model_name,
                "score": score,
                "url": u,
            })
            query_gpu += 1
            if max_items and len(new_items) >= max_items:
                break

        if verbose:
            print(f"  [{qi+1}/{len(SEARCH_QUERIES)}] '{query}': {len(raw)} listings, "
                  f"{query_gpu} new GPU (total {len(new_items)})", flush=True)
        time.sleep(random.uniform(*QUERY_DELAY_RANGE))  # be gentle between queries

    if stats is not None:
        stats["blocked"] = blocked
        stats["queries_run"] = queries_run
        stats["new"] = len(new_items)

    if blocked and verbose:
        print("  ⚠ Facebook is rate-limiting this session. It clears on its own after "
              "a while — the watch loop will back off and retry later.", flush=True)
    elif total_raw == 0 and verbose:
        print("  ⚠ Facebook returned 0 listings across every query — likely a login "
              "wall or rate-limit. Refresh your session with "
              "`python fb_marketplace.py --login`, or try again later.", flush=True)
    return new_items


def crawl_facebook(max_items: int = MAX_GPU_ITEMS, dry_run: bool = False,
                   mode: str = "full") -> None:
    """Standalone entry point: spins up its own headless browser, reuses the saved
    login session (fb_state.json) if present, runs the GPU scan, and appends new
    listings to fb_gpu.csv."""
    has_session = os.path.isfile(STATE_FILE)
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║   Facebook Marketplace GPU Crawler                           ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"Output    : {LOG_FILE if not dry_run else '(dry run — nothing saved)'}")
    print(f"Mode      : {mode}")
    print(f"Max items : {max_items}")
    print(f"Queries   : {len(SEARCH_QUERIES)}")
    print(f"Session   : {'fb_state.json (logged in)' if has_session else 'NONE — run --login'}")
    print()
    if not has_session:
        print("⚠ No saved login session — Facebook will likely throttle/limit results.")
        print("  Run once:  python fb_marketplace.py --login\n")

    known = set() if dry_run else load_existing_urls(LOG_FILE)
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    new_items: list[dict] = []
    with Stealth().use_sync(sync_playwright()) as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = make_fb_context(browser)
        page = ctx.new_page()

        try:
            new_items = crawl_facebook_gpu(page, known=known, max_items=max_items, mode=mode)
        finally:
            ctx.close()
            browser.close()

    # ── Save ──
    if new_items and not dry_run:
        log_listings(new_items, LOG_FILE)
        priced = sum(1 for it in new_items if it.get("price") is not None)
        print(f"\n✅ Saved {len(new_items)} new GPU listings to {LOG_FILE} "
              f"({priced} with a price)")
    elif new_items and dry_run:
        priced = sum(1 for it in new_items if it.get("price") is not None)
        print(f"\n📋 DRY RUN: {len(new_items)} new GPU listings found "
              f"({priced} with a price) — nothing saved")
    else:
        print(f"\n📭 No new GPU listings found.")

    if not dry_run and os.path.isfile(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            row_count = sum(1 for _ in csv.DictReader(f))
        print(f"\n── Summary ──")
        print(f"  Total rows in CSV: {row_count}")
        print(f"  This run added   : {len(new_items)}")
        print(f"  File             : {os.path.abspath(LOG_FILE)}")


def main():
    try:
        import applog
        applog.install("scoop")   # capture standalone FB runs to logs/ too
    except Exception:
        pass

    args = [a.lower() for a in sys.argv[1:]]

    if "--login" in args:
        login()
        return

    max_items = MAX_GPU_ITEMS
    dry_run = False
    mode = "full"

    for i, a in enumerate(args):
        if a in ("--dry-run", "-n"): dry_run = True
        elif a == "--watch": mode = "watch"
        elif a == "--full": mode = "full"
        elif a.startswith("--max-items="): max_items = int(a.split("=", 1)[1])
        elif a == "--max-items" and i + 1 < len(args): max_items = int(args[i + 1])
        elif a in ("--help", "-h"): print(__doc__); return

    try:
        crawl_facebook(max_items=max_items, dry_run=dry_run, mode=mode)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
