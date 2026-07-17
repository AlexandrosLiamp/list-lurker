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
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime

from listing_common import _norm, WANTED_KW, TRADE_KW  # shared with fb_marketplace

import ctypes
import requests as http_requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

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

# ── RAM config ────────────────────────────────────────────────────────────────
RAM_URL      = ("https://www.skroutz.gr/skoop/c/56/mnhmes-pc-ram.html"
                "?order_by=submitted_at&order_dir=desc")
RAM_LOG      = "ram_prices.csv"

RAM_THRESHOLDS = [
    ("16gb",  80),
    ("32gb", 130),
    ("64gb", 220),
    ("128gb", 380),
]
SODIMM_KW    = ["sodimm", "so-dimm", "laptop", "notebook"]
OLD_GEN_KW   = ["ddr3", "ddr2", "ddr1", "sdram", "pc100", "pc133", "pc2-", "pc3-"]
MIN_SPEED    = 3000   # MHz — listings with a stated speed below this are skipped

# ── GPU config ────────────────────────────────────────────────────────────────
GPU_URL      = ("https://www.skroutz.gr/skoop/c/55/kartes-grafikwn.html"
                "?order_by=submitted_at&order_dir=desc")
GPU_LOG      = "gpu_prices.csv"

# ── Motherboard & CPU config (for the PC Builder tool) ────────────────────────
MOBO_URL = ("https://www.skroutz.gr/skoop/c/31/motherboards-mhtrikes.html"
            "?order_by=submitted_at&order_dir=desc")
MOBO_LOG = "mobo_prices.csv"
CPU_URL  = ("https://www.skroutz.gr/skoop/c/32/cpu-epeksergastes.html"
            "?order_by=submitted_at&order_dir=desc")
CPU_LOG  = "cpu_prices.csv"

# ── Retail (main skroutz.gr catalog) URLs for each part ───────────────────────
RAM_RETAIL_URL  = "https://www.skroutz.gr/c/56/mnhmes-pc-ram.html"
RAM_RETAIL_LOG  = "ram_retail.csv"
CPU_RETAIL_URL  = "https://www.skroutz.gr/c/32/cpu-epeksergastes.html"
CPU_RETAIL_LOG  = "cpu_retail.csv"
MOBO_RETAIL_URL = "https://www.skroutz.gr/c/31/motherboards-mhtrikes.html"
MOBO_RETAIL_LOG = "mobo_retail.csv"
LAPTOP_RETAIL_URL = "https://www.skroutz.gr/c/25/laptop.html"
LAPTOP_RETAIL_LOG = "laptop_retail.csv"

# Minimum price/performance ratio to trigger a deal alert.
# PPR = perf_score / price_euros  e.g. score=100, price=280€ → PPR≈0.36
GPU_PPR_THRESHOLD = 0.370

# Mid-tier alert: ~3070-level cards (score 50–75) beating 3070 @ 220€ (PPR ≈ 0.282)
GPU_MIDTIER_SCORE_MIN = 50
GPU_MIDTIER_SCORE_MAX = 75
GPU_MIDTIER_PPR = round(62 / 220, 3)  # 0.282

# ── Data cleaning ─────────────────────────────────────────────────────────────
# Classifieds (esp. insomnia.gr) are dirty: wanted ads, trades, multi-item
# bundles, "browse my stock" placeholders at €1, broken cards. These skew every
# statistic, so we drop them at scrape time. _norm/WANTED_KW/TRADE_KW live in
# listing_common (shared with fb_marketplace) — imported at top of this file.

# Broken / parts-only — cheap for a reason.
BROKEN_KW = [
    "for parts", "for repair", "spare parts", "faulty", "broken", "not working",
    "not functional", "as is", "as-is", "dead gpu", "no display", "needs repair",
    "χαλασμ", "για επισκευη", "ανταλακτ", "ανταλλακτ", "μη λειτουργ",
    "δεν λειτουργ", "δε λειτουργ", "κατεστραμ", "καμεν", "προβλημα", "βλαβη", "ελαττωματ",
]
# Sold/ended and reserved markers (skoop/insomnia-specific — FB doesn't need these).
SOLD_KW   = ["πωληθηκε", "δοθηκε", "sold", "τελος -", "- τελος", "[τελος]", "(τελος)",
             "κρατημ", "κρατηθ", "δεσμευ", "reserved", "rezerv"]   # reserved = effectively gone

# Absolute price ceilings per kind — only catch true placeholders/typos, not legit
# halo products (Threadripper, 192GB kits, RTX 5090, etc.).
MAX_PRICE = {"gpu": 20000, "cpu": 20000, "ram": 8000, "mobo": 3000, "laptop": 15000}

def is_broken(text: str) -> bool:
    return any(kw in _norm(text) for kw in BROKEN_KW)

def _is_bundle(t: str) -> bool:
    """t must already be _norm()'d. True if several component categories are
    bundled in one listing (ambiguous price)."""
    cats = 0
    if re.search(r"(rtx|gtx|radeon|geforce|\brx ?\d|\barc )", t):                 cats += 1  # gpu
    if any(k in t for k in ("μητρικ", "motherboard", "am4", "am5", "lga",
                            "b450", "b550", "b650", "b660", "b760", "x570",
                            "x670", "z690", "z790", "z390")):                     cats += 1  # mobo
    if re.search(r"(ryzen|core i[3579]|\bi[3579][- ]?\d{3,5}|pentium|celeron|threadripper)", t): cats += 1  # cpu
    if any(k in t for k in ("ssd", "nvme", "hdd", "σκληρο")):                     cats += 1  # storage
    if any(k in t for k in ("psu", "τροφοδοτ", "power supply")):                  cats += 1  # psu
    if any(k in t for k in ("οθον", "monitor", "playstation", "xbox", "sony",
                            "setup", "ολοκληρο", "πληρες pc", "complete pc")):    cats += 1  # other
    has_sep = any(sep in t for sep in (",", "+", " και ", "κ.α", "κλπ"))
    return cats >= 2 and has_sep

def is_clean(name: str, price, kind: str, text: str | None = None) -> bool:
    """Return False for dirty/ambiguous classifieds listings.
    kind ∈ {'ram','gpu','cpu','mobo','laptop'}. `text` = full card text if richer than name."""
    t = _norm(text if text is not None else name)
    if any(k in t for k in WANTED_KW): return False
    if any(k in t for k in TRADE_KW):  return False
    if any(k in t for k in SOLD_KW):   return False
    if is_broken(t):                   return False
    if kind != "laptop" and _is_bundle(t): return False
    if price is not None:
        if price > MAX_PRICE.get(kind, 1e9): return False      # typo / placeholder / bundle ceiling
        if kind == "gpu" and price < 20: return False          # token / bait prices
        if kind in ("ram", "cpu", "mobo", "laptop") and price < 8: return False
    if kind == "ram":
        gens = sum(1 for g in ("ddr5", "ddr4", "ddr3", "ddr2") if g in t)
        if gens >= 2: return False                              # "browse my DDR4/DDR3/DDR2 stock"
        cap = max_capacity_gb(name)
        if cap and price and price / cap < 0.8: return False    # implausibly cheap → bundle/bait
    return True

def clean_listings(items: list[dict], kind: str) -> list[dict]:
    """Drop dirty/ambiguous classifieds listings for the given part kind."""
    return [it for it in items if is_clean(it.get("name", ""), it.get("price"), kind)]

# GPU performance scores live in gpu_perf.py (shared with negotiator.py — one source of truth).
from gpu_perf import GPU_MODELS, match_gpu, _GPU_RAW  # noqa: E402,F401  (_GPU_RAW kept for back-compat)

# ── GPU Retail config ─────────────────────────────────────────────────────────
GPU_RETAIL_URL = ("https://www.skroutz.gr/c/55/kartes-grafikwn/f/"
                  "694691_845785_845786_1216762/8GB-12GB-toulachiston-16gb-10GB.html")
GPU_RETAIL_LOG = "gpu_retail.csv"
RETAIL_SCAN_INTERVAL = 300  # rescan retail every 5 minutes
RETAIL_DROP_THRESHOLD = 0.10  # flag price drops >= 10%
RETAIL_DROP_MIN_EUR = 5.0     # minimum absolute drop to flag (exclude tiny drops)
FB_SCAN_INTERVAL = 600       # rescan Facebook every 10 min (heavier: 15 scrolled queries)
FB_BLOCK_COOLDOWN = 2700     # after an anti-bot block, wait 45 min before retrying FB

# ── Insomnia.gr config ────────────────────────────────────────────────────────
INSOMNIA_GPU_URL = "https://www.insomnia.gr/classifieds/category/11-kartes-grafikon/"
INSOMNIA_RAM_URL = "https://www.insomnia.gr/classifieds/category/47-mnimes/"

# ── Vendora.gr config ──────────────────────────────────────────────────────────
# Newest-first (sort=recent) + a price floor to skip junk. NOTE: the URL already
# carries query params, so pages are added with &page=N (see _vendora_page_url).
VENDORA_GPU_URL = ("https://vendora.gr/browse/qrzg1v/"
                   "ipologistes-tablets-ektipotes-periferiaka-exartimata-axesouar.html"
                   "?price_min=30&sort=recent")
# Vendora wraps back to page 1 once you scroll past page 50, so never crawl deeper.
VENDORA_MAX_PAGES = 50
VENDORA_GPU_LOG = "vendora_gpu.csv"
FB_GPU_LOG      = "fb_gpu.csv"

# ── Vinted.gr config ───────────────────────────────────────────────────────────
# Used-goods marketplace. Crawled via its JSON API (see vinted.py). One CSV per part.
#
# Background (2026-06-16): Vinted fronted the API with a Cloudflare managed challenge
# that this IP couldn't clear — the challenge solved but every follow-up request was
# re-challenged (an IP-reputation block), so the API only ever returned 401. Verified
# unbeatable from here via plain requests, curl_cffi Chrome impersonation, and
# headless+headful stealth Playwright. Such blocks are temporary; the IP cleared by
# 2026-06-20. See LIST LURKER/bugs/vinted-cloudflare-ip-block.md.
#
# SELF-HEALING GATE: VINTED_ENABLED is now tri-state instead of a hard on/off —
#   "on"            → force enabled (skip the probe)
#   "off"           → force disabled (silent)
#   "auto" / unset  → probe the API once at startup; enable only if this IP can reach
#                     it (see vinted.probe). This auto-recovers whenever the block
#                     lifts and mutes itself (one notice, no failure spam) when it's
#                     back, so no manual flipping is needed across the block's cycles.
def _parse_vinted_mode(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in ("1", "true", "yes", "on", "enabled"):
        return "on"
    if v in ("0", "false", "no", "off", "disabled"):
        return "off"
    return "auto"  # unset or "auto"

VINTED_MODE = _parse_vinted_mode(os.environ.get("VINTED_ENABLED", "auto"))
VINTED_GPU_LOG  = "vinted_gpu.csv"
VINTED_CPU_LOG  = "vinted_cpu.csv"
VINTED_RAM_LOG  = "vinted_ram.csv"
VINTED_MOBO_LOG = "vinted_mobo.csv"
VINTED_LOGS = {"gpu": VINTED_GPU_LOG, "cpu": VINTED_CPU_LOG,
               "ram": VINTED_RAM_LOG, "mobo": VINTED_MOBO_LOG}

# ── General ───────────────────────────────────────────────────────────────────
# Discord webhook for deal alerts. Resolution order: DISCORD_WEBHOOK env var, then
# "discord_webhook" in config.json (gitignored — copy config.example.json to create it).
# Empty → alerts are disabled; everything else still works.
def _load_discord_webhook() -> str:
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if hook:
        return hook
    import json
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            return str(json.load(fh).get("discord_webhook") or "").strip()
    except (OSError, ValueError):
        return ""

DISCORD_WEBHOOK = _load_discord_webhook()
SCAN_INTERVAL   = 60
PAGE_DELAY      = 2.5
NAV_TIMEOUT     = 20


# ── Deal helpers ──────────────────────────────────────────────────────────────

def parse_price(text: str) -> float | None:
    text = text.replace(".", "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def csv_price(raw) -> float | None:
    """Parse a price that was stored in one of our CSVs. Unlike parse_price — which is
    for RAW scraped European-formatted strings ("1.234,56 €") and so treats '.' as a
    thousands separator — CSV prices are already canonical floats ("140.0"), so a plain
    float() is correct. Falls back to parse_price for any legacy raw-formatted rows."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return parse_price(s)


def max_capacity_gb(name: str) -> int | None:
    nums = re.findall(r"(\d+)\s*gb", name, re.IGNORECASE)
    return max((int(n) for n in nums), default=None) if nums else None


def parse_speed_mhz(name: str) -> int | None:
    m = re.search(r"(\d{3,5})\s*(?:mhz|mt/s)", name, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r"ddr[45][-_\s](\d{3,5})", name, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r"τα[χx]ύτητα\s+(\d{3,5})", name, re.IGNORECASE)
    if m: return int(m.group(1))
    candidates = [int(x) for x in re.findall(r"\b(\d{4})\b", name)
                  if 2133 <= int(x) <= 9999]
    return max(candidates) if candidates else None


def is_desktop_ddr45(name: str) -> bool:
    n = name.lower()
    if any(kw in n for kw in SODIMM_KW):   return False
    if any(kw in n for kw in OLD_GEN_KW):  return False
    cap = max_capacity_gb(name)
    if cap is not None and cap < 16:        return False
    speed = parse_speed_mhz(name)
    if speed is not None and speed < MIN_SPEED: return False
    return True


def is_ram_deal(listing: dict) -> str | None:
    if listing["price"] is None or listing["price"] < 10 or not is_desktop_ddr45(listing["name"]):
        return None
    n = listing["name"].lower()
    for kw, threshold in RAM_THRESHOLDS:
        if kw in n and listing["price"] <= threshold:
            return f"{kw.upper()} ≤ {threshold}€"
    return None


# match_gpu is imported from gpu_perf (single source of truth for GPU scores).


# ── Layer 1: reject non-standalone-GPU listings ───────────────────────────────
# A GPU model name appears inside laptops, prebuilt PCs, mobile/MXM cards and
# accessories (waterblocks, brackets). Those must never count as a GPU deal. This
# heuristic is validated (see _classify_test.py) to keep real cards — including
# "ROG Strix Gaming"/"TUF Gaming" cards and VRAM-as-"GB RAM" listings — while
# catching the junk. Layer 2 (ai_verify) is the AI backstop for what slips through.
_L1_LAPTOP_KW = ("laptop", "notebook", "λαπτοπ", "φορητο", "macbook", "ideapad",
    "thinkpad", "thinkbook", "legion", "nitro 5", "nitro 16", "nitro 17", "victus",
    "predator helios", "predator triton", "aspire", "inspiron", "vivobook", "zenbook",
    "katana", "cyborg", "vector gp", "raider", "titan gt", "pulse gl", "alpha 15",
    "alpha 17", "bravo 15", "stealth 1", "summit e", "prestige 1", "tuf dash",
    "swift x", "blade 14", "blade 15", "blade 16", "blade 18", "galaxy book", "loq ")
# ROG laptops are Strix G14-G18 / Zephyrus / Flow — NOT the "ROG Strix Gaming" cards.
_L1_LAPTOP_RE = re.compile(r"\brog (zephyrus|flow)\b|\brog strix g1[4-8]\b|tuf gaming (a1[4-8]|f1[4-8])")
_L1_PREBUILT_KW = ("gaming pc", "gaming desktop", "desktop pc", "pc tower", "prebuilt",
    "pre-built", "pre built", "complete pc", "complete setup", "ολοκληρωμεν", "πληρες pc",
    "πληρες συστημα", "πληρες gaming", " setup ", "workstation", "gaming rig", "mid-tower",
    "mid tower", "midi tower", "full tower", "pc parts", "pc gaming", "gaming system",
    "συστημα υπολογ", "κουτι υπολογιστη", "budget gaming", "high-end gaming pc",
    "high end gaming pc", "midrange gaming", "υπολογιστης gaming")
_L1_MOBILE_KW = ("mxm", "mobile", "laptop gpu", "για laptop", "for laptop", "φορητου")
_L1_ACCESSORY_KW = ("waterblock", "water block", "water-block", "backplate", "back plate",
    "bracket", "riser", "anti-sag", "antisag", "ek-quantum", "ek quantum", "ek vector",
    "ekwb", "bykski", "alphacool", "barrow", "shroud", "βαση στηριξ", "box only",
    "empty box", "σκετο κουτι", "μονο το κουτι")
_L1_STORAGE_RE = re.compile(r"\b(ssd|hdd|nvme|\d{3,4}\s?gb (ssd|hdd|nvme)|\d\s?tb\b|σκληρο δισκ)")
_L1_CPU_RE = re.compile(r"\b(i[3579][- ]?\d{4,5}[a-z0-9]*|ryzen\s*[3579]\s*\d{3,4}[a-z0-9]*|threadripper|xeon|pentium|celeron)\b")
_L1_CPUFAM_RE = re.compile(r"\b(core\s*i[3579]|ryzen\s*[3579])\b")
_L1_RAM_RE = re.compile(r"\b\d{1,3}\s*gb\s*(ram|ddr[2345])\b|\bram\b")
_L1_RIG_RE = re.compile(r"\brig\b")


def classify_gpu_listing(name: str) -> tuple[str, str]:
    """Layer-1 heuristic. Returns ("card","") for a standalone desktop GPU, or
    ("reject", reason) for a laptop / prebuilt / mobile-MXM / accessory / system."""
    t = _norm(name)
    if any(k in t for k in _L1_LAPTOP_KW) or _L1_LAPTOP_RE.search(t): return "reject", "laptop"
    if any(k in t for k in _L1_MOBILE_KW):                            return "reject", "mobile/mxm"
    if any(k in t for k in _L1_ACCESSORY_KW):                         return "reject", "accessory"
    if any(k in t for k in _L1_PREBUILT_KW) or _L1_RIG_RE.search(t):  return "reject", "prebuilt/system"
    if _L1_STORAGE_RE.search(t):                                      return "reject", "has-storage→system"
    if _L1_CPU_RE.search(t):                                          return "reject", "has-cpu-model→system"
    if _L1_CPUFAM_RE.search(t) and _L1_RAM_RE.search(t):              return "reject", "cpu+ram→system"
    return "card", ""


def is_real_gpu_card(name: str) -> bool:
    """True if Layer 1 thinks this is a standalone desktop graphics card."""
    return classify_gpu_listing(name)[0] == "card"


def is_gpu_deal(listing: dict) -> tuple[str, float] | None:
    """Return (reason_str, ppr) if this GPU listing has PPR ≥ threshold.
    Layer 1: a deal must be a standalone desktop GPU card (not a laptop/prebuilt/
    accessory). Anything Layer 1 rejects never becomes a deal/alert."""
    if not listing["price"] or listing["price"] <= 1 or listing["price"] < 80:
        return None
    verdict, _reason = classify_gpu_listing(listing["name"])
    if verdict != "card":
        return None
    match = match_gpu(listing["name"])
    if not match:
        return None
    display, score = match
    ppr = score / listing["price"]
    if ppr >= GPU_PPR_THRESHOLD:
        return f"{display} | score {score}% | PPR {ppr:.3f}", ppr
    # Mid-tier alert: ~3070 performance band, beats 3070 @ 220€
    if GPU_MIDTIER_SCORE_MIN <= score <= GPU_MIDTIER_SCORE_MAX and ppr >= GPU_MIDTIER_PPR:
        return f"{display} | score {score}% | PPR {ppr:.3f} [mid-tier deal]", ppr
    return None


# ── Browser helpers ───────────────────────────────────────────────────────────

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


def extract_listings(page) -> list[dict]:
    listings = []
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

            # Skip sold cards — check badge/overlay and the full card text (accent-safe)
            try:
                if "πωληθηκε" in _norm(card.inner_text()):
                    continue
            except Exception:
                pass

            link_el = card.query_selector("a.link")
            href    = (link_el.get_attribute("href") or "") if link_el else ""
            url     = ("https://www.skroutz.gr" + href) if href.startswith("/") else href

            listings.append({"name": name, "condition": condition,
                              "price": price, "price_raw": price_raw, "url": url})
        except Exception:
            continue
    return listings


# ── Insomnia.gr scraping ─────────────────────────────────────────────────────

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


# ── Logging ───────────────────────────────────────────────────────────────────

def load_known_urls(log_file: str) -> set[str]:
    if not os.path.isfile(log_file):
        return set()
    known: set[str] = set()
    with open(log_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # `or ""` guards against malformed/short rows where DictReader yields
            # None for a missing trailing field (otherwise .strip() crashes).
            url = (row.get("url") or "").strip()
            if url: known.add(url)
    print(f"  {log_file}: {len(known)} existing URLs loaded")
    return known


def new_unique(items: list[dict], known: set[str]) -> list[dict]:
    """Items whose URL is neither already known nor a duplicate within this batch
    (guards against the same listing appearing twice on overlapping pages)."""
    out, seen = [], set()
    for it in items:
        u = (it.get("url") or "").strip()
        if u and u not in known and u not in seen:
            seen.add(u)
            out.append(it)
    return out


def _known_streak_checker(known: set[str], threshold: int | None):
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


# ── Discord ───────────────────────────────────────────────────────────────────

def send_discord(listing: dict, reason: str, extra_fields: list | None = None) -> None:
    if not DISCORD_WEBHOOK:
        return
    price_str = f"{listing['price']:.2f} €" if listing["price"] else "?"
    fields = [
        {"name": "Price",     "value": price_str,                  "inline": True},
        {"name": "Condition", "value": listing["condition"] or "–", "inline": True},
        {"name": "Deal",      "value": reason,                     "inline": False},
    ]
    if extra_fields:
        fields.extend(extra_fields)
    fields.append({"name": "Link", "value": listing["url"] or "–", "inline": False})
    embed = {
        "title": "🔥 Deal Found!",
        "description": listing["name"],
        "color": 0xFF6B6B,
        "fields": fields,
        "footer": {"text": f"Skroutz Skoop Monitor • {datetime.now().strftime('%H:%M:%S')}"},
    }
    try:
        resp = http_requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
        if resp.status_code not in (200, 204):
            print(f"  [Discord] {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        print(f"  [Discord] Send error: {e}")


# ── Crawl (generic) ───────────────────────────────────────────────────────────

def initial_crawl(bpage, already_known: set[str], base_url: str,
                  log_file: str, label: str,
                  log_filter=None, kind: str | None = None,
                  max_pages: int | None = None,
                  early_stop_after: int | None = None) -> set[str]:
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
    page1 = extract_listings(bpage)
    all_listings.extend(page1)
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
        listings = extract_listings(bpage)
        if not listings:
            print(f"  Page {n:2}/{total_pages}: empty — stopping", flush=True)
            break
        all_listings.extend(listings)
        print(f"  Page {n:2}/{total_pages}: {len(listings)} listings", flush=True)
        prev_hrefs = get_card_hrefs(bpage)
        stop_early = hit_old(listings)

    elapsed = time.time() - t0

    to_log = all_listings
    if kind:
        to_log = clean_listings(to_log, kind)
    if log_filter:
        to_log = [item for item in to_log if log_filter(item)]

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


# ── Watch loop ────────────────────────────────────────────────────────────────

def scan_page1_skroutz(bpage, url: str) -> list[dict]:
    try:
        bpage.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        print("  [scan] Reload timed out — skipping", flush=True)
        return []
    wait_for_cards(bpage, timeout=10000)
    return extract_listings(bpage)


def scan_page1_insomnia(bpage, url: str) -> list[dict]:
    if not _insomnia_goto(bpage, url, timeout=30000):
        print("  [scan] insomnia timeout — skipping", flush=True)
        return []
    return extract_insomnia_listings(bpage)


# ── Vendora.gr scanner ─────────────────────────────────────────────────────────
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
    no_progress = 0

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


# ── Vinted.gr scanner ──────────────────────────────────────────────────────────
# Vinted is a used-goods marketplace scraped via its JSON API (see vinted.py). The
# API needs an anonymous session cookie, so we cache one requests.Session and reuse
# it across pages and watch cycles (it self-refreshes on auth expiry).

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


# ── Facebook Marketplace scanner ───────────────────────────────────────────────
# Facebook needs a logged-in session and infinite scroll. The extraction, scrolling
# and login all live in fb_marketplace.py so there is ONE implementation; here we
# build a dedicated FB browser context (seeded with the saved session) and append
# new GPUs to fb_gpu.csv.


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


# ── Sold-listing verification ─────────────────────────────────────────────────

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
        kept = [r for r in rows if (r.get("url") or "").strip() not in sold]
        removed = len(rows) - len(kept)
        if removed:
            with open(lf, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["timestamp", "name", "condition", "price", "url"])
                w.writeheader()
                w.writerows(kept)
            print(f"  {lf}: removed {removed} sold rows")
            total_removed += removed

    print(f"Done in {time.time()-t0:.0f}s: {len(sold)} sold listings, {total_removed} CSV rows pruned.")


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


def _prune_urls(log_file: str, urls: set[str]) -> int:
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
            n = _prune_urls(log, sold)
            print(f"  → pruned {n} sold/closed row(s) from {log}")
    print("\nAI verification complete.")


# ── Wanted-ad purge (insomnia Ζήτηση listings) ────────────────────────────────

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
    import shutil
    from datetime import datetime as _dt
    t0 = time.time()
    wanted = set()
    wanted |= collect_wanted_insomnia(bpage, ctx, INSOMNIA_GPU_URL, "GPU")
    wanted |= collect_wanted_insomnia(bpage, ctx, INSOMNIA_RAM_URL, "RAM")
    print(f"\nFound {len(wanted)} unique wanted/trade listings on insomnia.", flush=True)
    if not wanted:
        print("Nothing to prune.")
        return
    bdir = "backup_csv_" + _dt.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(bdir, exist_ok=True)
    total = 0
    for log_file in (RAM_LOG, GPU_LOG):
        if os.path.isfile(log_file):
            shutil.copy(log_file, os.path.join(bdir, log_file))
            removed = _prune_urls(log_file, wanted)
            if removed:
                print(f"  {log_file}: removed {removed} wanted-ad row(s)")
            total += removed
    print(f"\nDone in {time.time()-t0:.0f}s: {total} CSV rows pruned. Backup: {bdir}")


# ── Skroutz retail (main site) scraping ──────────────────────────────────────

def extract_retail_listings(page) -> list[dict]:
    """Parse product cards from the main skroutz.gr catalog (not skoop)."""
    listings = []
    cards = page.query_selector_all('li[data-testid="sku-card"]')
    if not cards:
        print(f"    [retail] no cards found (title: {page.title()[:60]})", flush=True)
        return []

    for card in cards:
        try:
            name_el = card.query_selector('a[data-testid="sku-title-link"]')
            if not name_el:
                name_el = card.query_selector("h2 a.js-sku-link")
            name = name_el.inner_text().strip() if name_el else ""
            if not name:
                continue
            if is_broken(name):
                continue

            price_el = card.query_selector('div[data-testid="normal-price-container"] a.js-sku-link')
            if not price_el:
                price_el = card.query_selector(".price a.js-sku-link")
            price_raw = price_el.inner_text().strip() if price_el else ""
            price = parse_price(price_raw)

            # Strip tracking params — keep only /s/XXXXXXX/slug.html
            href = (name_el.get_attribute("href") or "") if name_el else ""
            clean = re.match(r"(/s/\d+/[^?]+)", href)
            href = clean.group(1) if clean else href
            url = "https://www.skroutz.gr" + href if href.startswith("/") else href

            listings.append({"name": name, "price": price, "price_raw": price_raw, "url": url})
        except Exception:
            continue
    return listings


def retail_next_page_url(page) -> str | None:
    """Return the next page URL from the <link rel='next'> tag in the head."""
    try:
        el = page.query_selector("link[rel='next']")
        if el:
            href = el.get_attribute("href") or ""
            if href:
                return href
    except Exception:
        pass
    return None


def crawl_retail(bpage, base_url: str, label: str, kind: str | None = None) -> list[dict]:
    """Crawl all pages of any retail skroutz.gr catalog and return cleaned listings."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] ── {label} RETAIL CRAWL (skroutz.gr) ──────────────────────────")
    t0 = time.time()
    all_listings: list[dict] = []

    try:
        bpage.goto(base_url, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightTimeout:
        print("  Timeout loading retail page — skipping", flush=True)
        return []
    time.sleep(2)

    page_num = 1
    while True:
        listings = extract_retail_listings(bpage)
        all_listings.extend(listings)
        print(f"  Page {page_num:2}: {len(listings)} products", flush=True)

        next_url = retail_next_page_url(bpage)
        if not next_url:
            break
        time.sleep(PAGE_DELAY)
        try:
            bpage.goto(next_url, wait_until="domcontentloaded", timeout=40000)
        except PlaywrightTimeout:
            print(f"  Page {page_num + 1}: timeout — stopping", flush=True)
            break
        time.sleep(1.5)
        page_num += 1

    if kind:
        all_listings = clean_listings(all_listings, kind)
    elapsed = time.time() - t0
    print(f"  Done in {elapsed:.0f}s: {len(all_listings)} retail products", flush=True)
    return all_listings


def crawl_retail_gpus(bpage) -> list[dict]:
    """Backwards-compatible wrapper — retail GPU crawl."""
    return crawl_retail(bpage, GPU_RETAIL_URL, "GPU", kind="gpu")


def log_retail(listings: list[dict], timestamp: str, log_file: str = GPU_RETAIL_LOG) -> None:
    """Overwrite the given retail CSV with the latest snapshot."""
    with open(log_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "name", "price", "url"])
        writer.writeheader()
        for item in listings:
            writer.writerow({"timestamp": timestamp, "name": item["name"],
                             "price": item["price"], "url": item["url"]})


def save_retail_snapshot(log_file: str) -> bool:
    """Copy current retail CSV to {name}_prev.csv before overwriting.
    Returns True if a snapshot was created."""
    if not os.path.exists(log_file):
        return False
    prev_file = log_file.replace(".csv", "_prev.csv")
    shutil.copy2(log_file, prev_file)
    return True


def detect_retail_drops(log_file: str) -> list[dict]:
    """Compare current retail CSV against its _prev.csv snapshot and return
    items whose price dropped >= RETAIL_DROP_THRESHOLD. Matches by URL."""
    prev_file = log_file.replace(".csv", "_prev.csv")
    if not os.path.exists(prev_file):
        return []

    prev_prices: dict[str, float] = {}
    with open(prev_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("url") or "").strip()
            try:
                price = float(row.get("price", 0))
            except (ValueError, TypeError):
                continue
            if url and price > 0:
                prev_prices[url] = price

    curr_data: dict[str, dict] = {}
    with open(log_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("url") or "").strip()
            name = (row.get("name") or "").strip()
            try:
                price = float(row.get("price", 0))
            except (ValueError, TypeError):
                continue
            if url and price > 0:
                curr_data[url] = {"name": name, "price": price}

    drops = []
    for url, curr in curr_data.items():
        old_price = prev_prices.get(url)
        if old_price is None:
            continue
        drop_eur = old_price - curr["price"]
        drop_pct = drop_eur / old_price
        if drop_pct >= RETAIL_DROP_THRESHOLD and drop_eur >= RETAIL_DROP_MIN_EUR:
            drops.append({
                "name": curr["name"],
                "url": url,
                "old_price": round(old_price, 2),
                "new_price": round(curr["price"], 2),
                "drop_pct": round(drop_pct * 100, 1),
                "drop_eur": round(drop_eur, 2),
            })

    drops.sort(key=lambda d: d["drop_pct"], reverse=True)
    return drops


def write_retail_deals(gpu_drops=None, ram_drops=None) -> None:
    """Write detected retail price drops to retail_deals.json for the dashboard.
    A category passed as None keeps the value already in the file, so a partial
    update (e.g. RAM scan succeeded, GPU failed) doesn't wipe the other side."""
    try:
        with open("retail_deals.json", encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, ValueError):
        existing = {}
    deals = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gpu": gpu_drops if gpu_drops is not None else existing.get("gpu", []),
        "ram": ram_drops if ram_drops is not None else existing.get("ram", []),
    }
    with open("retail_deals.json", "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)


def log_retail_laptops(listings: list[dict], timestamp: str, log_file: str = "laptop_retail.csv") -> None:
    """Parse, score, and write laptop listings to laptop_retail.csv."""
    import laptop_perf
    fieldnames = [
        "name", "price", "url", "timestamp",
        "cpu_name", "cpu_score", "gpu_name", "gpu_score",
        "ram_gb", "ssd_gb", "combined_score"
    ]
    with open(log_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in listings:
            scores = laptop_perf.get_laptop_scores(item["name"])
            row = {
                "name": item["name"],
                "price": item["price"],
                "url": item["url"],
                "timestamp": timestamp,
                "cpu_name": scores["cpu_name"],
                "cpu_score": scores["cpu_score"],
                "gpu_name": scores["gpu_name"],
                "gpu_score": scores["gpu_score"],
                "ram_gb": scores["ram_gb"],
                "ssd_gb": scores["ssd_gb"],
                "combined_score": scores["combined_score"]
            }
            writer.writerow(row)


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


def recreate_page(ctx):
    """Create a fresh page in the existing browser context."""
    try:
        page = ctx.new_page()
        print("  [recovery] New browser page created.", flush=True)
        return page
    except Exception as e:
        print(f"  [recovery] Failed to create new page: {e}", flush=True)
        return None


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


_snipe_proc = None


def _maybe_autosnipe():
    """If auto_snipe is enabled (the dashboard toggle in negotiator_config.json), launch a
    background negotiator snipe pass — it sends lowball offers on new Skoop GPU listings that
    pass the configured filters + AI gate. Guarded so only one runs at a time; never raises
    into the watch loop."""
    global _snipe_proc
    try:
        import json as _json
        import subprocess
        import sys as _sys
        cfg = _json.load(open("negotiator_config.json", encoding="utf-8"))
        if not cfg.get("auto_snipe"):
            return
        if _snipe_proc is not None and _snipe_proc.poll() is None:
            return  # previous snipe still running — don't stack
        _snipe_proc = subprocess.Popen(
            [_sys.executable, "negotiator.py", "snipe", "--confirm"],
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
    import os as _os
    if int(_os.environ.get("WATCH_RESTARTS", "0")) and time.time() - _BOOT > 1800:
        _os.environ["WATCH_RESTARTS"] = "0"


def _stall_watchdog(stall_limit: int = 180, check_every: int = 20) -> None:
    import os as _os
    while True:
        time.sleep(check_every)
        stalled = time.time() - _LAST_PROGRESS
        if stalled <= stall_limit:
            continue
        # avoid a tight restart loop if a source hangs immediately on every boot
        restarts = int(_os.environ.get("WATCH_RESTARTS", "0"))
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
        _os.environ["WATCH_RESTARTS"] = str(restarts + 1)
        # execv replaces this process image; the open pipes to the Playwright driver close,
        # so the wedged chromium is torn down and the fresh process starts clean.
        _os.execv(sys.executable, [sys.executable] + sys.argv)


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

EARLY_STOP_KNOWN = 10     # consecutive already-known listings → stop the crawl
WATCH_SEED_PAGES = 3      # pages to crawl before the watch loop takes over

# depth tier → (max_pages, early_stop_after)
_DEPTH_PARAMS = {
    "full":  (None, None),
    "crawl": (None, EARLY_STOP_KNOWN),
    "watch": (WATCH_SEED_PAGES, None),
}

ALL_PARTS = ("ram", "gpu", "cpu", "mobo", "laptop")
SOURCE_TOKENS = {"skoop", "insomnia", "vendora", "facebook", "vinted"}


def _gpu_logf(item):
    return match_gpu(item["name"]) is not None and is_real_gpu_card(item["name"])


def _parse_parts(token):
    """Resolve a part token into a set of parts. None/'all'/'parts' → everything;
    an unknown token → empty set (caller shows usage)."""
    if token in (None, "", "all", "parts"):
        return set(ALL_PARTS)
    if token in ALL_PARTS:
        return {token}
    return set()


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


def run_retail_crawl(bpage, parts):
    """Full crawl of the Skroutz RETAIL catalogs for `parts` (always every page)."""
    jobs = []
    if "gpu"  in parts: jobs.append((GPU_RETAIL_URL,  GPU_RETAIL_LOG,  "GPU",         "gpu"))
    if "ram"  in parts: jobs.append((RAM_RETAIL_URL,  RAM_RETAIL_LOG,  "RAM",         "ram"))
    if "cpu"  in parts: jobs.append((CPU_RETAIL_URL,  CPU_RETAIL_LOG,  "CPU",         "cpu"))
    if "mobo" in parts: jobs.append((MOBO_RETAIL_URL, MOBO_RETAIL_LOG, "Motherboard", "mobo"))
    if "laptop" in parts: jobs.append((LAPTOP_RETAIL_URL, LAPTOP_RETAIL_LOG, "Laptop", "laptop"))

    for log_file in (GPU_RETAIL_LOG, RAM_RETAIL_LOG):
        if log_file in [j[1] for j in jobs]:
            save_retail_snapshot(log_file)

    # None = category not scanned in this run; write_retail_deals preserves the
    # previous value for that category (same sentinel contract as watch_loop).
    gpu_drops, ram_drops = None, None
    for url, log_file, label, kind in jobs:
        items = crawl_retail(bpage, url, label, kind=kind)
        if items:
            if kind == "laptop":
                log_retail_laptops(items, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), log_file)
            else:
                log_retail(items, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), log_file)
            # Diff only when the crawl actually produced data (same reason as watch_loop).
            if kind == "gpu":
                gpu_drops = detect_retail_drops(GPU_RETAIL_LOG)
            elif kind == "ram":
                ram_drops = detect_retail_drops(RAM_RETAIL_LOG)

    if gpu_drops is not None or ram_drops is not None:
        write_retail_deals(gpu_drops, ram_drops)
        if gpu_drops:
            print(f"  [deals] {len(gpu_drops)} GPU price drops detected")
        if ram_drops:
            print(f"  [deals] {len(ram_drops)} RAM price drops detected")


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
        browser = pw.chromium.launch(headless=True)
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
