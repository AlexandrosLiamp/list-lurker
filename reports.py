"""
reports.py — user reports of abnormal / wrongly-listed items, stored per category.
────────────────────────────────────────────────────────────────────────────────────────────
You right-click a listing (in a table or on a matrix) and report it with a structured reason —
e.g. a RAM kit priced per-stick that our parser read as the total, or a "GPU" that's really a
bracket. Reports are saved by category in data_reports.json:

    { "gpu": [ {url,name,price,reason,note,source,at}, … ], "ram": [ … ], … }

Hardcoded payoff (now): `reported_urls()` is excluded by the negotiator's candidate finders and
hidden in the dashboard — bad listings stop polluting targeting and the data. The structured
`reason` makes deeper, hardcoded learning easy later (per-reason overrides, keyword mining).
"""

from __future__ import annotations

import json
import os
from datetime import datetime

FILE = "data_reports.json"
CATEGORIES = ("gpu", "ram", "cpu", "mobo", "retail", "other")


def _norm_cat(category: str) -> str:
    c = (category or "").strip().lower()
    return c if c in CATEGORIES else "other"


def load(path: str = FILE) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        data = json.loads(open(path, encoding="utf-8").read())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(data: dict, path: str = FILE) -> None:
    open(path, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))


def add(category: str, url: str, name: str, price, reason: str, note: str = "",
        source: str = "table", path: str = FILE) -> dict:
    """Record (or update) a report. Deduped by url within a category: a re-report updates the
    reason/note/timestamp rather than piling up duplicates."""
    cat = _norm_cat(category)
    data = load(path)
    bucket = data.setdefault(cat, [])
    try:
        price = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        price = None
    rec = {"url": (url or "").strip(), "name": (name or "").strip(), "price": price,
           "reason": (reason or "other").strip(), "note": (note or "").strip(),
           "source": source, "at": datetime.now().isoformat(timespec="seconds")}
    key = rec["url"] or (rec["name"] + "|" + str(price))
    existing = next((r for r in bucket if (r.get("url") or (r.get("name", "") + "|" + str(r.get("price")))) == key), None)
    if existing:
        existing.update(reason=rec["reason"], note=rec["note"], at=rec["at"], source=rec["source"])
    else:
        bucket.append(rec)
    save(data, path)
    return rec


def reported_urls(path: str = FILE) -> set[str]:
    """Flat set of every reported URL across all categories — for hardcoded exclusion."""
    out: set[str] = set()
    for bucket in load(path).values():
        for r in bucket:
            u = (r.get("url") or "").strip()
            if u:
                out.add(u)
    return out


def counts(path: str = FILE) -> dict:
    return {cat: len(bucket) for cat, bucket in load(path).items()}


def total(path: str = FILE) -> int:
    return sum(len(b) for b in load(path).values())


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"reports: {total()} total, by category: {counts()}")
    # group the latest by reason for a quick learning view
    for cat, bucket in load().items():
        by_reason: dict[str, int] = {}
        for r in bucket:
            by_reason[r.get("reason", "other")] = by_reason.get(r.get("reason", "other"), 0) + 1
        print(f"  {cat}: {by_reason}")
