"""Static configuration for the monitor: source URLs, CSV log paths, tunable
thresholds, keyword lists, and env-driven feature gates. Pure data — the only
runtime work is reading VINTED_ENABLED from the environment at import time."""

import os


# ── Skoop (Skroutz classifieds) URLs ──────────────────────────────────────────
RAM_URL  = ("https://www.skroutz.gr/skoop/c/56/mnhmes-pc-ram.html"
            "?order_by=submitted_at&order_dir=desc")
GPU_URL  = ("https://www.skroutz.gr/skoop/c/55/kartes-grafikwn.html"
            "?order_by=submitted_at&order_dir=desc")
MOBO_URL = ("https://www.skroutz.gr/skoop/c/31/motherboards-mhtrikes.html"
            "?order_by=submitted_at&order_dir=desc")
CPU_URL  = ("https://www.skroutz.gr/skoop/c/32/cpu-epeksergastes.html"
            "?order_by=submitted_at&order_dir=desc")

# ── Skroutz retail (main catalog) URLs ────────────────────────────────────────
RAM_RETAIL_URL    = "https://www.skroutz.gr/c/56/mnhmes-pc-ram.html"
CPU_RETAIL_URL    = "https://www.skroutz.gr/c/32/cpu-epeksergastes.html"
MOBO_RETAIL_URL   = "https://www.skroutz.gr/c/31/motherboards-mhtrikes.html"
LAPTOP_RETAIL_URL = "https://www.skroutz.gr/c/25/laptop.html"
GPU_RETAIL_URL    = ("https://www.skroutz.gr/c/55/kartes-grafikwn/f/"
                     "694691_845785_845786_1216762/8GB-12GB-toulachiston-16gb-10GB.html")

# ── Other used-goods sources ──────────────────────────────────────────────────
INSOMNIA_GPU_URL = "https://www.insomnia.gr/classifieds/category/11-kartes-grafikon/"
INSOMNIA_RAM_URL = "https://www.insomnia.gr/classifieds/category/47-mnimes/"

# Newest-first (sort=recent) + a price floor to skip junk. NOTE: the URL already
# carries query params, so pages are added with &page=N (see _vendora_page_url).
VENDORA_GPU_URL = ("https://vendora.gr/browse/qrzg1v/"
                   "ipologistes-tablets-ektipotes-periferiaka-exartimata-axesouar.html"
                   "?price_min=30&sort=recent")
# Vendora wraps back to page 1 once you scroll past page 50, so never crawl deeper.
VENDORA_MAX_PAGES = 50

# ── CSV log paths ─────────────────────────────────────────────────────────────
RAM_LOG  = "ram_prices.csv"
GPU_LOG  = "gpu_prices.csv"
CPU_LOG  = "cpu_prices.csv"
MOBO_LOG = "mobo_prices.csv"

RAM_RETAIL_LOG    = "ram_retail.csv"
CPU_RETAIL_LOG    = "cpu_retail.csv"
MOBO_RETAIL_LOG   = "mobo_retail.csv"
LAPTOP_RETAIL_LOG = "laptop_retail.csv"
GPU_RETAIL_LOG    = "gpu_retail.csv"

VENDORA_GPU_LOG = "vendora_gpu.csv"
FB_GPU_LOG      = "fb_gpu.csv"

VINTED_GPU_LOG  = "vinted_gpu.csv"
VINTED_CPU_LOG  = "vinted_cpu.csv"
VINTED_RAM_LOG  = "vinted_ram.csv"
VINTED_MOBO_LOG = "vinted_mobo.csv"
VINTED_LOGS = {"gpu": VINTED_GPU_LOG, "cpu": VINTED_CPU_LOG,
               "ram": VINTED_RAM_LOG, "mobo": VINTED_MOBO_LOG}

# ── Deal thresholds ───────────────────────────────────────────────────────────
RAM_THRESHOLDS = [
    ("16gb",  80),
    ("32gb", 130),
    ("64gb", 220),
    ("128gb", 380),
]

# PPR = perf_score / price_euros  (e.g. score=100, price=280€ → PPR≈0.36)
GPU_PPR_THRESHOLD = 0.370

# Mid-tier alert: ~3070-level cards (score 50–75) beating 3070 @ 220€.
GPU_MIDTIER_SCORE_MIN = 50
GPU_MIDTIER_SCORE_MAX = 75
GPU_MIDTIER_PPR = round(62 / 220, 3)  # 0.282

# ── Retail-drop detection ─────────────────────────────────────────────────────
RETAIL_SCAN_INTERVAL  = 300   # rescan retail every 5 minutes
RETAIL_DROP_THRESHOLD = 0.10  # flag price drops >= 10%
RETAIL_DROP_MIN_EUR   = 5.0   # minimum absolute drop (excludes tiny drops)

# ── Scan cadence ──────────────────────────────────────────────────────────────
SCAN_INTERVAL     = 60
PAGE_DELAY        = 2.5
NAV_TIMEOUT       = 20
FB_SCAN_INTERVAL  = 600    # rescan Facebook every 10 min (15 scrolled queries)
FB_BLOCK_COOLDOWN = 2700   # after an anti-bot block, wait 45 min before retrying

# ── Cleaning ceilings + keyword lists ─────────────────────────────────────────
# Absolute per-kind price ceilings: catch true placeholders/typos while keeping
# halo products legit (Threadripper, 192GB kits, RTX 5090, etc.).
MAX_PRICE = {"gpu": 20000, "cpu": 20000, "ram": 8000, "mobo": 3000, "laptop": 15000}

# Broken / parts-only — cheap for a reason.
BROKEN_KW = [
    "for parts", "for repair", "spare parts", "faulty", "broken", "not working",
    "not functional", "as is", "as-is", "dead gpu", "no display", "needs repair",
    "χαλασμ", "για επισκευη", "ανταλακτ", "ανταλλακτ", "μη λειτουργ",
    "δεν λειτουργ", "δε λειτουργ", "κατεστραμ", "καμεν", "προβλημα", "βλαβη", "ελαττωματ",
]
# Sold/ended and reserved markers (skoop/insomnia-specific — FB doesn't need these).
SOLD_KW = ["πωληθηκε", "δοθηκε", "sold", "τελος -", "- τελος", "[τελος]", "(τελος)",
           "κρατημ", "κρατηθ", "δεσμευ", "reserved", "rezerv"]

# ── Vinted feature gate ───────────────────────────────────────────────────────
# Tri-state VINTED_ENABLED env var — Vinted's Cloudflare-fronted API sometimes
# IP-blocks this host, so we self-heal by probing at startup rather than requiring
# a manual flip. See bugs/vinted-cloudflare-ip-block.md and vinted_enabled().
#   "on"            → force enabled (skip the probe)
#   "off"           → force disabled (silent)
#   "auto" / unset  → probe the API once at startup; enable only if reachable.
def _parse_vinted_mode(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in ("1", "true", "yes", "on", "enabled"):
        return "on"
    if v in ("0", "false", "no", "off", "disabled"):
        return "off"
    return "auto"


VINTED_MODE = _parse_vinted_mode(os.environ.get("VINTED_ENABLED", "auto"))
