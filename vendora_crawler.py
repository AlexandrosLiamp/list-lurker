"""
Vendora.gr GPU listing crawler
───────────────────────────────
Crawls the Vendora.gr browse page (computers/tablets/printers/peripherals/accessories)
and extracts only GPU listings, saving them to a CSV.

Usage:
    python vendora_crawler.py                        # crawl all pages
    python vendora_crawler.py --max-pages 50         # crawl first 50 pages only
    python vendora_crawler.py --max-pages 5 --dry    # preview first 5 pages, no CSV
"""

import csv
import os
import re
import sys
import time
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = ("https://vendora.gr/browse/qrzg1v/"
            "ipologistes-tablets-ektipotes-periferiaka-exartimata-axesouar.html")
LOG_FILE = "vendora_gpu.csv"
PAGE_DELAY = 1.5      # seconds between page requests
REQUEST_TIMEOUT = 30  # seconds

# ── GPU model database (same as monitor.py) ────────────────────────────────────
_GPU_RAW: dict[str, tuple[str, int]] = {
    "rtx 5090":        ("GeForce RTX 5090",        199),
    "rtx 4090":        ("GeForce RTX 4090",         152),
    "rtx 5080":        ("GeForce RTX 5080",         131),
    "rtx 4080 super":  ("GeForce RTX 4080 SUPER",   117),
    "rtx 4080":        ("GeForce RTX 4080",         116),
    "rx 7900 xtx":     ("Radeon RX 7900 XTX",       116),
    "rtx 5070 ti":     ("GeForce RTX 5070 Ti",      114),
    "rx 9070 xt":      ("Radeon RX 9070 XT",        109),
    "rx 7900 xt":      ("Radeon RX 7900 XT",        100),
    "rtx 3090 ti":     ("GeForce RTX 3090 Ti",       98),
    "rtx 4070 ti super": ("GeForce RTX 4070 Ti SUPER", 98),
    "rx 9070":         ("Radeon RX 9070",            98),
    "rtx 4070 ti":     ("GeForce RTX 4070 Ti",       90),
    "rtx 5070":        ("GeForce RTX 5070",          89),
    "rtx 3090":        ("GeForce RTX 3090",          88),
    "rtx 3080 ti":     ("GeForce RTX 3080 Ti",       86),
    "rtx 4070 super":  ("GeForce RTX 4070 SUPER",    84),
    "rx 7900 gre":     ("Radeon RX 7900 GRE",        82),
    "rx 6950 xt":      ("Radeon RX 6950 XT",         81),
    "rtx 4070":        ("GeForce RTX 4070",          80),
    "rx 6900 xt":      ("Radeon RX 6900 XT",         79),
    "rtx 3080":        ("GeForce RTX 3080",          78),
    "rx 7800 xt":      ("Radeon RX 7800 XT",         78),
    "rx 6800 xt":      ("Radeon RX 6800 XT",         75),
    "rtx 5060 ti 16":  ("GeForce RTX 5060 Ti 16GB",  69),
    "rtx 5060 ti 8":   ("GeForce RTX 5060 Ti 8GB",   69),
    "rtx 5060 ti":     ("GeForce RTX 5060 Ti",       69),
    "rx 7700 xt":      ("Radeon RX 7700 XT",         68),
    "rx 9060 xt 16":   ("Radeon RX 9060 XT 16GB",    66),
    "rtx 3070 ti":     ("GeForce RTX 3070 Ti",       66),
    "rx 6800":         ("Radeon RX 6800",            64),
    "rx 9060 xt 8":    ("Radeon RX 9060 XT 8GB",     62),
    "rx 9060 xt":      ("Radeon RX 9060 XT",         62),
    "rtx 3070":        ("GeForce RTX 3070",          62),
    "rtx 2080 ti":     ("GeForce RTX 2080 Ti",       61),
    "rtx 4060 ti 16":  ("GeForce RTX 4060 Ti 16GB",  61),
    "rtx 4060 ti 8":   ("GeForce RTX 4060 Ti 8GB",   61),
    "rtx 4060 ti":     ("GeForce RTX 4060 Ti",       61),
    "rtx 5060":        ("GeForce RTX 5060",          60),
    "rx 6750 xt":      ("Radeon RX 6750 XT",         58),
    "rtx 3060 ti":     ("GeForce RTX 3060 Ti",       54),
    "rx 6700 xt":      ("Radeon RX 6700 XT",         53),
    "rtx 2080 super":  ("GeForce RTX 2080 SUPER",    49),
    "rx 7600 xt":      ("Radeon RX 7600 XT",         49),
    "rtx 4060":        ("GeForce RTX 4060",          49),
    "arc b580":        ("Arc B580",                  49),
    "rtx 5050":        ("GeForce RTX 5050",          47),
    "rtx 2080":        ("GeForce RTX 2080",          47),
    "rx 6650 xt":      ("Radeon RX 6650 XT",         46),
    "rx 7600":         ("Radeon RX 7600",            46),
    "rtx 2070 super":  ("GeForce RTX 2070 SUPER",    44),
    "gtx 1080 ti":     ("GeForce GTX 1080 Ti",       43),
    "arc a770":        ("Arc A770",                  43),
    "rtx 3060 12":     ("GeForce RTX 3060 12GB",     42),
    "rx 6600 xt":      ("Radeon RX 6600 XT",         41),
    "radeon vii":      ("Radeon VII",                41),
    "arc a750":        ("Arc A750",                  40),
    "rx 5700 xt":      ("Radeon RX 5700 XT",         39),
    "rtx 2070":        ("GeForce RTX 2070",          39),
    "rx 6600":         ("Radeon RX 6600",            37),
    "arc a580":        ("Arc A580",                  36),
    "rtx 2060 super":  ("GeForce RTX 2060 SUPER",    36),
    "rx vega 64":      ("Radeon RX Vega 64",         34),
    "rtx 2060":        ("GeForce RTX 2060",          33),
    "rx 5700":         ("Radeon RX 5700",            33),
    "gtx 1080":        ("GeForce GTX 1080",          32),
    "gtx 1070 ti":     ("GeForce GTX 1070 Ti",       32),
    "rx 5600 xt":      ("Radeon RX 5600 XT",         31),
    "rx vega 56":      ("Radeon RX Vega 56",         30),
    "gtx 1070":        ("GeForce GTX 1070",          29),
    "gtx 1660 super":  ("GeForce GTX 1660 SUPER",    27),
    "gtx 1660 ti":     ("GeForce GTX 1660 Ti",       27),
    "gtx 980 ti":      ("GeForce GTX 980 Ti",        26),
    "rtx 3050 8":      ("GeForce RTX 3050 8GB",      26),
    "rtx 3050":        ("GeForce RTX 3050",          26),
    "r9 fury x":       ("Radeon R9 FURY X",          25),
    "gtx 1660":        ("GeForce GTX 1660",          25),
    "rx 590":          ("Radeon RX 590",             24),
    "r9 fury":         ("Radeon R9 FURY",            23),
    "gtx 980":         ("GeForce GTX 980",           23),
    "gtx 1650 super":  ("GeForce GTX 1650 SUPER",    23),
    "rx 6500 xt":      ("Radeon RX 6500 XT",         22),
    "rx 5500 xt":      ("Radeon RX 5500 XT",         22),
    "rx 580":          ("Radeon RX 580",             22),
    "gtx 1060 6":      ("GeForce GTX 1060 6GB",      21),
    "r9 390x":         ("Radeon R9 390X",            21),
    "gtx 690":         ("GeForce GTX 690",           21),
    "rx 480":          ("Radeon RX 480",             21),
    "hd 7990":         ("Radeon HD 7990",            21),
    "gtx 780 ti":      ("GeForce GTX 780 Ti",        20),
    "gtx 970":         ("GeForce GTX 970",           20),
}
GPU_MODELS = {k: v for k, v in sorted(_GPU_RAW.items(), key=lambda x: len(x[0]), reverse=True)}

# ── Additional GPU detection keywords (Greek + generic) ───────────────────────
GPU_KEYWORDS = [
    "κάρτα γραφικών", "καρτα γραφικων", "κάρτα γραφικων",
    "karta grafikon", "karta grafikwn",
    "gpu", "graphics card", "video card", "vga",
]

# Brands / lines that strongly indicate a GPU even without explicit "κάρτα γραφικών"
GPU_BRAND_KW = [
    "geforce", "radeon", "nvidia", "amd", "intel arc",
    "rtx ", "gtx ", "rx ", "quadro", "tesla", "firepro",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Strip Greek accents and lowercase."""
    s = str(s or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def parse_price(text: str) -> float | None:
    """Extract a euro amount from text like '€ 870' or '€250'."""
    text = text.replace(",", ".").replace("\xa0", " ")
    m = re.search(r"€\s*([\d.]+)", text)
    if not m:
        m = re.search(r"([\d.]+)\s*€", text)
    return float(m.group(1)) if m else None


def match_gpu(name: str) -> tuple[str, int] | None:
    """Return (display_name, score) for the first matching GPU model, or None."""
    n = name.lower()
    for key, (display, score) in GPU_MODELS.items():
        if key in n:
            return display, score
    return None


def is_gpu_listing(name: str) -> tuple[str, int] | None:
    """Determine if a listing title refers to a GPU. Returns (model_name, score)
    if it's a recognised GPU, or a fallback match if it clearly mentions GPU
    keywords but the model isn't in our database (score = 0)."""
    n = _norm(name)

    # First: try exact model match
    match = match_gpu(name)
    if match:
        return match

    # Second: check for GPU keywords + brand indicators
    has_gpu_kw = any(kw in n for kw in GPU_KEYWORDS)
    has_brand = any(kw in n for kw in GPU_BRAND_KW)
    # Also check for model-number patterns (e.g. "RTX 3060" might appear without "GeForce")
    has_model_num = bool(re.search(r"\b(rx|rtx|gtx|gt|hd|r[579])\s*\d", n))

    if has_gpu_kw and (has_brand or has_model_num):
        return ("GPU (unrecognised model)", 0)

    # Fallback: if title mentions "κάρτα γραφικών" + a number, it's probably a GPU
    if "καρτα γραφικ" in n and re.search(r"\d+\s*gb", n):
        return ("GPU (unrecognised model)", 0)

    return None


def extract_condition(name: str) -> str:
    """Extract condition from the listing title."""
    n = _norm(name)
    if "καινουργιο" in n:
        return "Καινουργιο"
    if "σαν καινουργιο" in n or "san kenourgio" in n:
        return "Σαν καινουργιο"
    if "metachirismeno" in n or "metachirismeni" in n or "μεταχειρισμεν" in n or "used" in n:
        return "Μεταχειρισμενο"
    return ""


# ── Page fetching ─────────────────────────────────────────────────────────────

session = requests.Session()
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
})


def fetch_page(url: str) -> str | None:
    """Fetch a page and return its HTML, or None on failure."""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"    [error] {e}", flush=True)
        return None


def parse_listings(html: str) -> list[dict]:
    """Extract all listing cards from a page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    # Each listing is an <a class="card vCard card-product">
    for card in soup.select("a.card.vCard.card-product"):
        try:
            href = card.get("href", "")
            if not href:
                continue

            # Title
            title_el = card.select_one("p.title span.body-m")
            name = title_el.get_text(strip=True) if title_el else ""
            if not name:
                continue

            # Price
            price_el = card.select_one("span.label-l.tc-petrol-800")
            price_raw = price_el.get_text(strip=True) if price_el else ""
            price = parse_price(price_raw)

            # Condition from title
            condition = extract_condition(name)

            listings.append({
                "name": name,
                "price": price,
                "price_raw": price_raw,
                "condition": condition,
                "url": href,
            })
        except Exception:
            continue
    return listings


def get_next_page_url(html: str) -> str | None:
    """Get the next page URL from the <link rel='next'> tag or the button."""
    soup = BeautifulSoup(html, "html.parser")
    # Prefer <link rel="next"> (in <head>)
    link_next = soup.select_one("link[rel='next']")
    if link_next:
        href = link_next.get("href", "")
        if href:
            return href
    # Fallback: "Δες περισσότερα" button
    btn = soup.select_one("a.next-page-button")
    if btn:
        href = btn.get("href", "")
        if href:
            return href
    return None


# ── Logging ───────────────────────────────────────────────────────────────────

def load_existing_urls(log_file: str) -> set[str]:
    if not os.path.isfile(log_file):
        return set()
    known: set[str] = set()
    with open(log_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()   # guard against short/None rows
            if url:
                known.add(url)
    print(f"  {log_file}: {len(known)} existing GPU listings")
    return known


def log_listings(listings: list[dict], log_file: str) -> None:
    file_exists = os.path.isfile(log_file)
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "name", "condition", "price", "model", "score", "url",
        ])
        if not file_exists:
            writer.writeheader()
        for item in listings:
            writer.writerow({
                "timestamp": item["timestamp"],
                "name": item["name"],
                "condition": item["condition"],
                "price": item["price"],
                "model": item["model"],
                "score": item["score"],
                "url": item["url"],
            })


# ── Main crawl ────────────────────────────────────────────────────────────────

def crawl_vendora(max_pages: int | None = None, dry_run: bool = False) -> None:
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║     Vendora.gr GPU Listing Crawler                          ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print(f"Source    : {BASE_URL}")
    print(f"Output    : {LOG_FILE if not dry_run else '(dry run — no CSV)'}")
    if max_pages:
        print(f"Max pages : {max_pages}")
    print()

    known = set() if dry_run else load_existing_urls(LOG_FILE)
    all_gpu: list[dict] = []
    page_num = 1
    url = f"{BASE_URL}?page=1"
    consecutive_empty = 0

    while url:
        if max_pages and page_num > max_pages:
            print(f"\nReached max_pages limit ({max_pages}).", flush=True)
            break

        print(f"  Page {page_num}...", end=" ", flush=True)
        html = fetch_page(url)
        if not html:
            print("FAILED", flush=True)
            consecutive_empty += 1
            if consecutive_empty >= 3:
                print("  3 consecutive failures — stopping.", flush=True)
                break
            page_num += 1
            url = f"{BASE_URL}?page={page_num}"
            continue

        listings = parse_listings(html)
        print(f"{len(listings)} listings", end="", flush=True)

        # Filter to GPU only
        gpu_found = 0
        new_gpu = 0
        for item in listings:
            match = is_gpu_listing(item["name"])
            if match:
                gpu_found += 1
                model_name, score = match
                item["model"] = model_name
                item["score"] = score
                item["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                is_new = item["url"] not in known
                if is_new:
                    known.add(item["url"])
                    if not dry_run:
                        all_gpu.append(item)
                    new_gpu += 1

        print(f", {gpu_found} GPU, {new_gpu} new", flush=True)
        consecutive_empty = 0

        # Next page
        next_url = get_next_page_url(html)
        if not next_url:
            print("  No next page link found — reached the end.", flush=True)
            break
        url = next_url
        page_num += 1
        time.sleep(PAGE_DELAY)

    # Log all GPU listings at once
    if all_gpu and not dry_run:
        log_listings(all_gpu, LOG_FILE)
        print(f"\n✅ Saved {len(all_gpu)} new GPU listings to {LOG_FILE}")
    elif all_gpu and dry_run:
        print(f"\n📋 DRY RUN: {len(all_gpu)} GPU listings would be saved")
    else:
        print(f"\n📭 No new GPU listings found.")

    # Summary
    total_seen = page_num - 1
    print(f"\n── Summary ──")
    print(f"  Pages scanned : {total_seen}")
    print(f"  Total GPU in  : vendora_gpu.csv (if not dry run)")
    if not dry_run and os.path.isfile(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            row_count = sum(1 for _ in csv.DictReader(f))
        print(f"  Total rows    : {row_count}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = [a.lower() for a in sys.argv[1:]]
    max_pages = None
    dry_run = False

    for a in args:
        if a == "--dry" or a == "-n":
            dry_run = True
        elif a.startswith("--max-pages="):
            max_pages = int(a.split("=", 1)[1])
        elif a.startswith("--max-pages") or a.startswith("-p"):
            # next arg
            pass

    # Handle --max-pages N (separate arg)
    for i, a in enumerate(args):
        if a in ("--max-pages", "-p") and i + 1 < len(args):
            try:
                max_pages = int(args[i + 1])
            except ValueError:
                pass

    try:
        crawl_vendora(max_pages=max_pages, dry_run=dry_run)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
