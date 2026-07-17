"""
ram_specs.py — single source of truth for parsing and sanity-checking RAM listings.

Second-hand RAM titles are the messiest data in the pipeline: kit notation ("2x8GB"),
per-stick prices on kit listings, SODIMMs sold as desktop RAM, and impossible spec
combos ("DDR4 6000MHz"). Every RAM heuristic lives here; monitor.py imports these.

Ground truth: `ram_kits.json`, generated from the pc-part-dataset project's ~13.5k
real memory kits (MIT — https://github.com/docyx/pc-part-dataset). The dataset clone
under repos/ is only needed to (re)build the JSON, never at runtime.

    python ram_specs.py build   # repos/pc-part-dataset → ram_kits.json
    python ram_specs.py scan    # flag every row of the local RAM CSVs, print a report
"""

from __future__ import annotations

import csv
import json
import os
import re
import statistics
from functools import lru_cache

KITS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ram_kits.json")
DATASET_FILE = os.path.join("repos", "pc-part-dataset", "data", "json", "memory.json")

SODIMM_KW = ["sodimm", "so-dimm", "laptop", "notebook"]
OLD_GEN_KW = ["ddr3", "ddr2", "ddr1", "sdram", "pc100", "pc133", "pc2-", "pc3-"]
MIN_SPEED = 3000   # MHz — desktop-deal floor; listings stating less are skipped

# Real module sizes in GB. Gates bare numbers ("16g", the M in "NxM") so speeds,
# timings and CAS values are never misread as capacities.
MODULE_GB = {1, 2, 4, 8, 16, 24, 32, 48, 64, 96, 128}

# JEDEC/XMP plausibility per DDR gen — floor/ceiling merged with the observed speeds
# in ram_kits.json, and the fallback when the JSON is missing.
GEN_MHZ_RANGE = {2: (400, 1200), 3: (800, 2400), 4: (1866, 5100), 5: (4400, 8800)}

_KIT_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)\s*(?:gb|gib|g\b)?", re.IGNORECASE)
_GB_RE = re.compile(r"(\d+)\s*g(?:i?b)\b", re.IGNORECASE)
_BARE_G_RE = re.compile(r"(\d+)\s*g\b", re.IGNORECASE)
_GEN_RE = re.compile(r"ddr\s?(\d)", re.IGNORECASE)


def parse_speed_mhz(name: str) -> int | None:
    m = re.search(r"(\d{3,5})\s*(?:mhz|mt/s)", name, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r"ddr[2345][-_\s](\d{3,5})", name, re.IGNORECASE)
    if m: return int(m.group(1))
    m = re.search(r"τα[χx]ύτητα\s+(\d{3,5})", name, re.IGNORECASE)
    if m: return int(m.group(1))
    candidates = [int(x) for x in re.findall(r"\b(\d{4})\b", name)
                  if 2133 <= int(x) <= 9999]
    return max(candidates) if candidates else None


def parse_ram_title(name: str) -> dict:
    """Parse a messy listing title. `total_gb` is kit-aware: "2x8GB" → 16, not 8."""
    n = (name or "").lower()
    m = _GEN_RE.search(n)
    gen = int(m.group(1)) if m else None

    kit, kit_gb = None, 0
    for m in _KIT_RE.finditer(n):
        count, size = int(m.group(1)), int(m.group(2))
        if 1 <= count <= 8 and size in MODULE_GB and count * size > kit_gb:
            kit, kit_gb = (count, size), count * size
    explicit = [int(m.group(1)) for m in _GB_RE.finditer(n)]
    explicit += [int(m.group(1)) for m in _BARE_G_RE.finditer(n)
                 if int(m.group(1)) in MODULE_GB]
    explicit_gb = max(explicit) if explicit else None

    return {
        "gen": gen,
        "mhz": parse_speed_mhz(name),
        "kit": kit,
        "explicit_gb": explicit_gb,
        "total_gb": max(kit_gb, explicit_gb or 0) or None,
        "sodimm": any(kw in n for kw in SODIMM_KW),
        "old_gen": any(kw in n for kw in OLD_GEN_KW) or (gen is not None and gen < 4),
    }


def max_capacity_gb(name: str) -> int | None:
    return parse_ram_title(name)["total_gb"]


def is_desktop_ddr45(name: str) -> bool:
    p = parse_ram_title(name)
    if p["sodimm"] or p["old_gen"]:                  return False
    if p["total_gb"] is not None and p["total_gb"] < 16:  return False
    if p["mhz"] is not None and p["mhz"] < MIN_SPEED:     return False
    return True


@lru_cache(maxsize=1)
def _load_kits() -> dict:
    try:
        return json.load(open(KITS_FILE, encoding="utf-8"))
    except OSError:
        return {}


def _speed_gen_mismatch(gen, mhz) -> bool:
    if gen is None or mhz is None:
        return False
    lo, hi = GEN_MHZ_RANGE.get(gen, (0, 99999))
    speeds = _load_kits().get("gen_speeds", {}).get(str(gen))
    if speeds:
        lo, hi = min(lo, speeds[0]), max(hi, speeds[-1])
    return not (lo <= mhz <= hi)


def check_ram(name: str, price: float | None = None,
              median_eur_per_gb: float | None = None) -> list[str]:
    """Sanity flags for one listing; empty list = plausible. A flagged listing must
    never fire a deal alert — bad data, not a bargain."""
    p = parse_ram_title(name)
    flags = []
    if p["sodimm"]:
        flags.append("sodimm")
    if p["old_gen"]:
        flags.append("old_gen")
    if _speed_gen_mismatch(p["gen"], p["mhz"]):
        flags.append("speed_gen_mismatch")
    if p["kit"] and p["explicit_gb"] and \
            p["explicit_gb"] not in (p["kit"][0] * p["kit"][1], p["kit"][1]):
        flags.append("capacity_kit_mismatch")
    if price and p["total_gb"] and median_eur_per_gb and \
            price / p["total_gb"] < 0.4 * median_eur_per_gb:
        flags.append("per_stick_price_suspect")   # kit specs at a fraction of market €/GB
    return flags


@lru_cache(maxsize=4)
def median_eur_per_gb(csv_path: str = "ram_prices.csv") -> float | None:
    """Median €/GB over the collected listings — the baseline that exposes per-stick
    prices. ponytail: cached per run; the median drifts far slower than a watch loop."""
    try:
        rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    except OSError:
        return None
    vals = []
    for r in rows:
        try:
            price = float(r.get("price") or "")
        except ValueError:
            continue
        gb = parse_ram_title(r.get("name", ""))["total_gb"]
        if gb and price > 0:
            vals.append(price / gb)
    return statistics.median(vals) if len(vals) >= 20 else None


# ── CLI: build / scan ─────────────────────────────────────────────────────────

def build(dataset_path: str = DATASET_FILE) -> dict:
    data = json.load(open(dataset_path, encoding="utf-8"))
    gen_speeds: dict[int, set] = {}
    combos = set()
    for kit in data:
        speed = kit.get("speed")
        gen, mhz = speed if isinstance(speed, list) and len(speed) == 2 else (None, None)
        mods = kit.get("modules") or []
        if gen and mhz:
            gen_speeds.setdefault(int(gen), set()).add(int(mhz))
        if len(mods) == 2 and all(mods):
            combos.add((int(mods[0]), int(mods[1])))
    out = {
        "source": "https://github.com/docyx/pc-part-dataset (MIT)",
        "kits": len(data),
        "gen_speeds": {str(g): sorted(s) for g, s in sorted(gen_speeds.items())},
        "module_combos": sorted(combos),
    }
    json.dump(out, open(KITS_FILE, "w", encoding="utf-8"), separators=(",", ":"))
    return out


def scan(paths=("ram_prices.csv", "vinted_ram.csv")) -> None:
    for path in paths:
        try:
            rows = list(csv.DictReader(open(path, encoding="utf-8")))
        except OSError:
            continue
        med = median_eur_per_gb(path)   # per-source median: Vinted runs cheaper than Skoop
        print(f"\nmedian €/GB ({path}): {med:.2f}" if med else f"\nmedian €/GB ({path}): n/a")
        counts: dict[str, int] = {}
        flagged = []
        for r in rows:
            try:
                price = float(r.get("price") or "")
            except ValueError:
                price = None
            flags = check_ram(r.get("name", ""), price, med)
            for f in flags:
                counts[f] = counts.get(f, 0) + 1
            if flags:
                flagged.append((flags, (r.get("name") or "")[:70], price))
        print(f"{path}: {len(rows)} rows, {len(flagged)} flagged  {counts}")
        for flags, name, price in flagged[:10]:
            print(f"  [{','.join(flags)}] {price}€  {name}")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "build":
        out = build()
        print(f"ram_kits.json: {out['kits']} kits, "
              f"gens {list(out['gen_speeds'])}, {len(out['module_combos'])} module combos")
    else:
        scan()
