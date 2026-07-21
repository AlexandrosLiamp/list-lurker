"""Deal-detection logic: does a scraped listing beat our thresholds?

RAM deals: DDR4/DDR5 desktop kits at or below the per-capacity price ceiling
in config.RAM_THRESHOLDS, gated by ram_specs sanity checks so implausibly
cheap "bundle" prices don't fire alerts.

GPU deals: two-layer defense against the false-positive mess of scraping
mixed classifieds. Layer 1 (classify_gpu_listing) rejects laptops, prebuilt
PCs, mobile/MXM cards, and accessories that merely *mention* a GPU model;
only surviving listings run through match_gpu + PPR (perf-score / price)
against the config thresholds. Layer 2 (AI verification) is the runtime
backstop, applied by the watch loop — not here.
"""

import re

from listing_common import _norm
from config import (RAM_THRESHOLDS, GPU_PPR_THRESHOLD,
                    GPU_MIDTIER_SCORE_MIN, GPU_MIDTIER_SCORE_MAX, GPU_MIDTIER_PPR)
from gpu_perf import match_gpu
import ram_specs
from ram_specs import is_desktop_ddr45


def is_ram_deal(listing: dict) -> str | None:
    if listing["price"] is None or listing["price"] < 10 or not is_desktop_ddr45(listing["name"]):
        return None
    if ram_specs.check_ram(listing["name"], listing["price"],
                           ram_specs.median_eur_per_gb()):
        return None   # implausible specs or per-stick-suspect price — bad data, not a deal
    n = listing["name"].lower()
    for kw, threshold in RAM_THRESHOLDS:
        if kw in n and listing["price"] <= threshold:
            return f"{kw.upper()} ≤ {threshold}€"
    return None


# ── Layer 1: reject non-standalone-GPU listings ───────────────────────────────
# A GPU model name appears inside laptops, prebuilt PCs, mobile/MXM cards and
# accessories (waterblocks, brackets). Those must never count as a GPU deal. This
# heuristic is validated to keep real cards — including "ROG Strix Gaming"/"TUF
# Gaming" cards and VRAM-as-"GB RAM" listings — while catching the junk. Layer 2
# (ai_verify, applied by the watch loop) is the AI backstop for what slips through.
_L1_LAPTOP_KW = ("laptop", "notebook", "λαπτοπ", "φορητο", "macbook", "ideapad",
    "thinkpad", "thinkbook", "legion", "nitro 5", "nitro 16", "nitro 17", "victus",
    "predator helios", "predator triton", "aspire", "inspiron", "vivobook", "zenbook",
    "katana", "cyborg", "vector gp", "raider", "titan gt", "pulse gl", "alpha 15",
    "alpha 17", "bravo 15", "stealth 1", "summit e", "prestige 1", "tuf dash",
    "swift x", "blade 14", "blade 15", "blade 16", "blade 18", "galaxy book", "loq ")
# ROG laptops are Strix G14-G18 / Zephyrus / Flow — NOT the "ROG Strix Gaming" cards.
_L1_LAPTOP_RE = re.compile(r"\brog (zephyrus|flow)\b|\brog strix g1[4-8]\b|tuf gaming (a1[4-8]|f1[4-8])")
_L1_PREBUILT_KW = ("gaming pc", "gaming desktop", "desktop pc", "pc tower", "prebuilt",
    "pre-built", "pre built", "complete pc", "complete setup", "ολοκληρωμεν", "πληρες pc",
    "πληρες συστημα", "πληρες gaming", " setup ", "workstation", "gaming rig", "mid-tower",
    "mid tower", "midi tower", "full tower", "pc parts", "pc gaming", "gaming system",
    "συστημα υπολογ", "κουτι υπολογιστη", "budget gaming", "high-end gaming pc",
    "high end gaming pc", "midrange gaming", "υπολογιστης gaming")
_L1_MOBILE_KW = ("mxm", "mobile", "laptop gpu", "για laptop", "for laptop", "φορητου")
_L1_ACCESSORY_KW = ("waterblock", "water block", "water-block", "backplate", "back plate",
    "bracket", "riser", "anti-sag", "antisag", "ek-quantum", "ek quantum", "ek vector",
    "ekwb", "bykski", "alphacool", "barrow", "shroud", "βαση στηριξ", "box only",
    "empty box", "σκετο κουτι", "μονο το κουτι")
_L1_STORAGE_RE = re.compile(r"\b(ssd|hdd|nvme|\d{3,4}\s?gb (ssd|hdd|nvme)|\d\s?tb\b|σκληρο δισκ)")
_L1_CPU_RE = re.compile(r"\b(i[3579][- ]?\d{4,5}[a-z0-9]*|ryzen\s*[3579]\s*\d{3,4}[a-z0-9]*|threadripper|xeon|pentium|celeron)\b")
_L1_CPUFAM_RE = re.compile(r"\b(core\s*i[3579]|ryzen\s*[3579])\b")
_L1_RAM_RE = re.compile(r"\b\d{1,3}\s*gb\s*(ram|ddr[2345])\b|\bram\b")
_L1_RIG_RE = re.compile(r"\brig\b")


def classify_gpu_listing(name: str) -> tuple[str, str]:
    """Layer-1 heuristic. Returns ("card","") for a standalone desktop GPU, or
    ("reject", reason) for a laptop / prebuilt / mobile-MXM / accessory / system."""
    t = _norm(name)
    if any(k in t for k in _L1_LAPTOP_KW) or _L1_LAPTOP_RE.search(t): return "reject", "laptop"
    if any(k in t for k in _L1_MOBILE_KW):                            return "reject", "mobile/mxm"
    if any(k in t for k in _L1_ACCESSORY_KW):                         return "reject", "accessory"
    if any(k in t for k in _L1_PREBUILT_KW) or _L1_RIG_RE.search(t):  return "reject", "prebuilt/system"
    if _L1_STORAGE_RE.search(t):                                      return "reject", "has-storage→system"
    if _L1_CPU_RE.search(t):                                          return "reject", "has-cpu-model→system"
    if _L1_CPUFAM_RE.search(t) and _L1_RAM_RE.search(t):              return "reject", "cpu+ram→system"
    return "card", ""


def is_real_gpu_card(name: str) -> bool:
    """True if Layer 1 thinks this is a standalone desktop graphics card."""
    return classify_gpu_listing(name)[0] == "card"


def is_gpu_deal(listing: dict) -> tuple[str, float] | None:
    """Return (reason_str, ppr) if this GPU listing has PPR ≥ threshold.
    Layer 1: a deal must be a standalone desktop GPU card (not a laptop/prebuilt/
    accessory). Anything Layer 1 rejects never becomes a deal/alert."""
    if not listing["price"] or listing["price"] <= 1 or listing["price"] < 80:
        return None
    verdict, _reason = classify_gpu_listing(listing["name"])
    if verdict != "card":
        return None
    match = match_gpu(listing["name"])
    if not match:
        return None
    display, score = match
    ppr = score / listing["price"]
    if ppr >= GPU_PPR_THRESHOLD:
        return f"{display} | score {score}% | PPR {ppr:.3f}", ppr
    # Mid-tier alert: ~3070 performance band, beats 3070 @ 220€
    if GPU_MIDTIER_SCORE_MIN <= score <= GPU_MIDTIER_SCORE_MAX and ppr >= GPU_MIDTIER_PPR:
        return f"{display} | score {score}% | PPR {ppr:.3f} [mid-tier deal]", ppr
    return None
