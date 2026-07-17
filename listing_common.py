"""
listing_common.py — helpers and vocabulary shared by every marketplace crawler.

Kept tiny on purpose: only put things here that at least two crawlers need
(currently monitor.py + fb_marketplace.py). If it's used in one place, keep it
in that place.
"""

import unicodedata


def _norm(s: str) -> str:
    """Lowercase + strip Greek accents so keyword checks match regardless of tonos."""
    s = str(s or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


# Wanted ('ζητώ' / looking-to-buy) and trade-only ('ανταλλαγή') ads aren't real
# sales — their price is meaningless (often a €1 placeholder).
WANTED_KW = ["ζητειται", "ζητηση", "ζητουνται", "ζητω ", "wanted", "αγοραζω", "psaxno"]
TRADE_KW  = ["ανταλλαγ", "ανταλαγ", "ανταλλασσ", "swap", "trade", "exchange"]


def is_wanted_or_trade(name: str) -> bool:
    n = _norm(name)
    return any(k in n for k in WANTED_KW) or any(k in n for k in TRADE_KW)
