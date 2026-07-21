"""Price parsing — the two variants and why they exist separately.

parse_price handles RAW scraped European-formatted strings ("1.234,56 €"): '.'
is a thousands separator, ',' is the decimal. csv_price handles values re-read
from our own CSVs — canonical floats like "140.0". Mixing them up caused the
historical 10x price-inflation bug (parse_price("140.0") → 1400.0), so csv_price
tries plain float() first and only falls back to parse_price for legacy rows."""

import re


def parse_price(text: str) -> float | None:
    text = text.replace(".", "").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def csv_price(raw) -> float | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return parse_price(s)
