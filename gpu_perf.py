"""
gpu_perf.py — GPU model → relative performance score (single source of truth).
────────────────────────────────────────────────────────────────────────────────────────────
Used by monitor.py (deal alerts, PPR = score/price) and negotiator.py (deal discovery by
performance / price-per-performance). Keep the table here so both stay in sync.

`match_gpu(name)` → (display_name, score) for the first matching model, longest key first.
`score_for(name)` → just the score (or None).
"""

from __future__ import annotations

# key (lowercase, no brand prefix) → (display_name, perf_score)
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
    "rtx 5060 ti":     ("GeForce RTX 5060 Ti",       69),  # ambiguous VRAM fallback
    "rx 7700 xt":      ("Radeon RX 7700 XT",         68),
    "rx 9060 xt 16":   ("Radeon RX 9060 XT 16GB",    66),
    "rtx 3070 ti":     ("GeForce RTX 3070 Ti",       66),
    "rx 6800":         ("Radeon RX 6800",            64),
    "rx 9060 xt 8":    ("Radeon RX 9060 XT 8GB",     62),
    "rx 9060 xt":      ("Radeon RX 9060 XT",         62),  # ambiguous fallback
    "rtx 3070":        ("GeForce RTX 3070",          62),
    "rtx 2080 ti":     ("GeForce RTX 2080 Ti",       61),
    "rtx 4060 ti 16":  ("GeForce RTX 4060 Ti 16GB",  61),
    "rtx 4060 ti 8":   ("GeForce RTX 4060 Ti 8GB",   61),
    "rtx 4060 ti":     ("GeForce RTX 4060 Ti",       61),  # ambiguous fallback
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

# Longest key first so more-specific models (e.g. "rtx 4060 ti") match before "rtx 4060".
GPU_MODELS = {k: v for k, v in sorted(_GPU_RAW.items(), key=lambda x: len(x[0]), reverse=True)}


def match_gpu(name: str) -> tuple[str, int] | None:
    """Return (display_name, score) for the first matching GPU model, or None."""
    n = (name or "").lower()
    for key, (display, score) in GPU_MODELS.items():
        if key in n:
            return display, score
    return None


def score_for(name: str) -> int | None:
    m = match_gpu(name)
    return m[1] if m else None


import re as _re

# Plausible desktop-GPU VRAM sizes (GB) — used to pick VRAM out of a listing title without
# mistaking a clock/model number for memory.
_VRAM_SIZES = {1, 2, 3, 4, 6, 8, 10, 11, 12, 16, 20, 24, 32, 48}


def vram_gb(name: str) -> int | None:
    """Best-effort VRAM in GB parsed from a GPU listing title (e.g. 'RX 5700 XT 8GB' → 8).
    Returns the largest plausible '<n>gb' token, or None if none is found."""
    hits = [int(m) for m in _re.findall(r"(\d{1,2})\s*gb", (name or "").lower())]
    hits = [h for h in hits if h in _VRAM_SIZES]
    return max(hits) if hits else None


def score_band(score: int, lo: float = 0.85, hi: float = 1.18) -> tuple[int, int]:
    """A performance band 'around' a score (default ≈ −15%…+18%), for 'cards like an X' queries."""
    return (int(score * lo), int(round(score * hi)))


_BRANDS = ("geforce", "radeon", "rtx", "gtx", "rx", "arc", "hd", "r9")


def _norm(s: str) -> str:
    return _re.sub(r"[^a-z0-9]", "", (s or "").lower())


# (variant_string, display, score) for loose matching — both the full key and a brandless form
# ("rx 5700 xt" → "rx5700xt" and "5700xt"), longest variant first so specific models win.
def _build_loose():
    out = []
    for key, (display, score) in GPU_MODELS.items():
        nk = _norm(key)
        toks = key.split()
        bl = _norm("".join(toks[1:])) if toks and toks[0] in _BRANDS else nk
        for v in {nk, bl}:
            if len(v) >= 4:           # avoid 3-char collisions like "580" matching a price
                out.append((v, display, score))
    out.sort(key=lambda x: len(x[0]), reverse=True)
    return out


_LOOSE = _build_loose()


def find_model_loose(text: str):
    """Match a GPU model named loosely in free text (e.g. '5700xt', 'rtx3070') → (display, score).
    Normalizes away spaces/brand so user phrasing matches the canonical keys. None if nothing fits."""
    nt = _norm(text)
    for variant, display, score in _LOOSE:
        if variant in nt:
            return display, score
    return None
