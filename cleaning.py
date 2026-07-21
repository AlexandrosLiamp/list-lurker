"""Drop dirty/ambiguous classifieds listings at scrape time.

Classifieds (especially insomnia.gr) are full of noise — wanted ads, trades,
multi-item bundles, "browse my stock" placeholders at €1, broken cards — that
would skew every downstream statistic. Kill it at the source: filter here on
name + price + kind and never write it to CSV in the first place."""

import re

from listing_common import _norm, WANTED_KW, TRADE_KW
from config import BROKEN_KW, SOLD_KW, MAX_PRICE
from ram_specs import max_capacity_gb


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
