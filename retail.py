"""Skroutz RETAIL catalog scraping (the main skroutz.gr site, not skoop).

Retail crawls are always full (no early-stop tier). Each run overwrites the
CSV — save_retail_snapshot backs the previous run into *_prev.csv so
detect_retail_drops can diff URL-by-URL and surface price drops for the
dashboard. write_retail_deals uses a per-category None-sentinel so a failed
scan on one side never wipes drops the other side legitimately produced."""

import csv
import json
import os
import shutil
import time
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from prices import parse_price
from cleaning import is_broken, clean_listings
from config import (PAGE_DELAY, RETAIL_DROP_THRESHOLD, RETAIL_DROP_MIN_EUR,
                    GPU_RETAIL_URL, GPU_RETAIL_LOG,
                    RAM_RETAIL_URL, RAM_RETAIL_LOG,
                    CPU_RETAIL_URL, CPU_RETAIL_LOG,
                    MOBO_RETAIL_URL, MOBO_RETAIL_LOG,
                    LAPTOP_RETAIL_URL, LAPTOP_RETAIL_LOG)

import re


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
