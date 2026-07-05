"""
Vinted.gr catalog crawler (used-goods marketplace)
──────────────────────────────────────────────────
Vinted exposes a clean public JSON API for catalog browsing. Listings are NOT
needed from the rendered HTML — we prime an anonymous session (one GET to the
homepage sets the `access_token_web` cookie) and then call:

    /api/v2/catalog/items?catalog_ids=<id>&page=<n>&per_page=<n>&order=newest_first

`order=newest_first` makes the feed newest-first, so monitor.py's early-stop
(consecutive already-known listings) works exactly like the Skroutz/Vendora feeds.

This module is intentionally thin: it only fetches + normalises listings to the
common schema used across sources:

    {name, condition, price, price_raw, url}

All GPU model-matching, cleaning, deal detection and CSV logging stay in
monitor.py (reusing its single GPU database), just like the Vendora source.

Every Vinted catalog is a USED-goods listing — the per-item `status` field carries
the seller's condition label (e.g. "Νέο με ετικέτα", "Πολύ καλό", "Καλό").

Catalog IDs (from the user's URLs):
    GPU 3602 · CPU 3599 · RAM 3603 · Motherboard 3600
"""

import re

BASE = "https://www.vinted.gr"
HOME = BASE + "/"
API = BASE + "/api/v2/catalog/items"

# part → Vinted catalog id
CATALOG_IDS = {
    "gpu":  3602,
    "cpu":  3599,
    "ram":  3603,
    "mobo": 3600,
}
PART_BY_CATALOG = {cid: part for part, cid in CATALOG_IDS.items()}

PER_PAGE = 96            # Vinted's max page size — fewer requests
ORDER = "newest_first"   # newest-first feed → enables early-stop on known listings
TIMEOUT = 30

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
}


def make_session():
    """Create a requests session primed with Vinted's anonymous cookies. One GET
    to the homepage sets `access_token_web`, which the catalog API requires."""
    import requests
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:
        s.get(HOME, timeout=TIMEOUT)
    except Exception:
        pass
    return s


def catalog_url(part: str, page: int = 1) -> str:
    """The catalog URL for a part. Carries &order=newest_first so it sorts newest-first
    (the API fetch enforces this too via fetch_page; kept here for parity/links)."""
    return f"{BASE}/catalog?catalog[]={CATALOG_IDS[part]}&page={page}&order={ORDER}"


def catalog_id_from_url(url: str) -> int | None:
    """Extract the catalog id from a Vinted catalog URL (…catalog[]=3602…)."""
    m = re.search(r"catalog(?:\[\])?=(\d+)", url or "")
    return int(m.group(1)) if m else None


def _normalize(items: list[dict]) -> list[dict]:
    """Map raw API items to the common listing schema used across sources."""
    out: list[dict] = []
    for it in items:
        try:
            url = (it.get("url") or "").strip()
            name = (it.get("title") or "").strip()
            if not url or not name:
                continue
            price_obj = it.get("price")
            if isinstance(price_obj, dict):
                amount = price_obj.get("amount")
                currency = price_obj.get("currency_code", "")
            else:
                amount, currency = price_obj, ""
            try:
                price = float(amount) if amount not in (None, "") else None
            except (TypeError, ValueError):
                price = None
            price_raw = f"{amount} {currency}".strip() if amount not in (None, "") else ""
            condition = (it.get("status") or "").strip()
            out.append({
                "name": name,
                "condition": condition,
                "price": price,
                "price_raw": price_raw,
                "url": url,
            })
        except Exception:
            continue
    return out


def fetch_page(catalog_id: int, page: int = 1, *, session=None,
               per_page: int = PER_PAGE, order: str = ORDER) -> list[dict]:
    """Fetch one catalog page from the Vinted API and return normalised listings.

    Creates a session if none is given. On an auth failure (expired anon token)
    the same session's cookies are refreshed once and the request retried. Returns
    [] on any failure so callers can treat it like an empty page."""
    s = session or make_session()
    url = (f"{API}?catalog_ids={catalog_id}&page={page}"
           f"&per_page={per_page}&order={order}")
    for attempt in (1, 2):
        try:
            r = s.get(url, timeout=TIMEOUT,
                      headers={"Accept": "application/json",
                               "X-Requested-With": "XMLHttpRequest"})
            if r.status_code in (401, 403) and attempt == 1:
                # Anonymous token likely expired — refresh cookies and retry once.
                try:
                    s.get(HOME, timeout=TIMEOUT)
                except Exception:
                    pass
                continue
            r.raise_for_status()
            data = r.json()
            return _normalize(data.get("items", []) or [])
        except Exception:
            if attempt == 2:
                return []
    return []


def fetch_by_url(url: str, page: int = 1, *, session=None) -> list[dict]:
    """Convenience: fetch page 1 (or `page`) for the catalog referenced by a Vinted
    catalog URL. Used by monitor.py's watch loop where categories carry a URL."""
    cid = catalog_id_from_url(url)
    if cid is None:
        return []
    return fetch_page(cid, page=page, session=session)


def probe(session=None) -> bool:
    """Cheap reachability check: can this IP currently reach the Vinted API?

    Fetches a single GPU-catalog item and returns True iff it comes back non-empty.
    The catalogs are never genuinely empty (96 listings/page), so an empty result
    means the request was blocked or failed — i.e. the Cloudflare IP-reputation block
    is active. monitor.py's auto-gate calls this so Vinted self-heals across the block
    coming and going (enabled when the IP is clean, muted when it's blocked)."""
    s = session or make_session()
    return bool(fetch_page(CATALOG_IDS["gpu"], page=1, session=s, per_page=1))
