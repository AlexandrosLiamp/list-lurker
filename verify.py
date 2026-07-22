"""Manual / on-demand AI verification of deal candidates.

Wraps the ai_verify SDK-facing module with the CSV-plumbing that turns "the
GPU_LOG has 200 rows, most already sold" into "here are the URLs that still
qualify as deals, verify each, prune the sold ones". Called from monitor.py's
`aiverify` CLI verb — kept out of monitor.py so the entry point stays a thin
dispatcher.
"""

import csv
import os

import ai_verify
from config import RAM_LOG, GPU_LOG
from crawl_utils import prune_urls
from deals import is_gpu_deal, is_ram_deal
from prices import csv_price


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
            n = prune_urls(log_file, sold)
            print(f"  → pruned {n} sold/closed row(s) from {log_file}")
    print("\nAI verification complete.")
