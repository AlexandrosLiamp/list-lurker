"""
laptop_perf.py — Mobile CPU/GPU lookup tables and title parsing engine.
────────────────────────────────────────────────────────────────────────
Used to match, score, and evaluate laptops found in Greek marketplaces.
Exposes:
  - `parse_laptop_title(title)` -> Dictionary of parsed specs and scores.
"""

from __future__ import annotations
import re
import unicodedata

# ────────────────────────────────────────────────────────────────────────
# 1. CPU LOOKUP DATABASE (Normalized Key -> (Display Name, Relative Score))
# ────────────────────────────────────────────────────────────────────────
CPU_DATABASE: dict[str, tuple[str, int]] = {
    # Intel Core 14th Gen (HX series)
    "i914900hx": ("Intel Core i9-14900HX", 195),
    "i714700hx": ("Intel Core i7-14700HX", 160),
    "i514450hx": ("Intel Core i5-14450HX", 112),
    
    # Intel Core 13th Gen (HX / H / U series)
    "i913980hx": ("Intel Core i9-13980HX", 190),
    "i913900hx": ("Intel Core i9-13900HX", 185),
    "i913900h":  ("Intel Core i9-13900H", 140),
    "i713700hx": ("Intel Core i7-13700HX", 150),
    "i713700h":  ("Intel Core i7-13700H", 135),
    "i713620h":  ("Intel Core i7-13620H", 125),
    "i513500h":  ("Intel Core i5-13500H", 108),
    "i513420h":  ("Intel Core i5-13420H", 85),
    "i71355u":   ("Intel Core i7-1355U", 75),
    "i51335u":   ("Intel Core i5-1335U", 70),
    "i31315u":   ("Intel Core i3-1315U", 50),
    
    # Intel Core Ultra (Meteor Lake)
    "coreultra9185h": ("Intel Core Ultra 9 185H", 148),
    "coreultra7155h": ("Intel Core Ultra 7 155H", 130),
    "coreultra5125h": ("Intel Core Ultra 5 125H", 105),
    "coreultra7155u": ("Intel Core Ultra 7 155U", 82),
    "coreultra5125u": ("Intel Core Ultra 5 125U", 72),
    
    # Intel Core 12th Gen (H / P / U series)
    "i912900hk": ("Intel Core i9-12900HK", 135),
    "i912900h":  ("Intel Core i9-12900H", 130),
    "i712800h":  ("Intel Core i7-12800H", 122),
    "i712700h":  ("Intel Core i7-12700H", 115),
    "i712650h":  ("Intel Core i7-12650H", 106),
    "i512600h":  ("Intel Core i5-12600H", 102),
    "i512500h":  ("Intel Core i5-12500H", 98),
    "i512450h":  ("Intel Core i5-12450H", 80),
    "i71260p":   ("Intel Core i7-1260P", 88),
    "i51240p":   ("Intel Core i5-1240P", 82),
    "i71255u":   ("Intel Core i7-1255U", 72),
    "i51235u":   ("Intel Core i5-1235U", 68),
    "i31215u":   ("Intel Core i3-1215U", 48),

    # Intel Core 11th Gen
    "i911900h":  ("Intel Core i9-11900H", 98),
    "i711800h":  ("Intel Core i7-11800H", 82),
    "i511400h":  ("Intel Core i5-11400H", 60),
    "i71165g7":  ("Intel Core i7-1165G7", 48),
    "i51135g7":  ("Intel Core i5-1135G7", 45),
    "i31115g4":  ("Intel Core i3-1115G4", 32),

    # Intel Core 10th Gen
    "i910980hk": ("Intel Core i9-10980HK", 78),
    "i710875h":  ("Intel Core i7-10875H", 68),
    "i710750h":  ("Intel Core i7-10750H", 52),
    "i510300h":  ("Intel Core i5-10300H", 40),
    "i710510u":  ("Intel Core i7-10510U", 30),
    "i510210u":  ("Intel Core i5-10210U", 28),
    "i310110u":  ("Intel Core i3-10110U", 20),
    
    # Intel Budget Alder Lake-N
    "i3n305":    ("Intel Core i3-N305", 35),

    # AMD Ryzen 8000 Series (Zen 4 Hawk Point)
    "ryzen98945hs": ("AMD Ryzen 9 8945HS", 155),
    "ryzen78840hs": ("AMD Ryzen 7 8840HS", 132),
    "ryzen78840u":  ("AMD Ryzen 7 8840U", 112),
    "ryzen58640hs": ("AMD Ryzen 5 8640HS", 105),
    "ryzen58640u":  ("AMD Ryzen 5 8640U", 92),

    # AMD Ryzen 7000 Series (Zen 4 / Zen 3 / Zen 2)
    "ryzen97945hx": ("AMD Ryzen 9 7945HX", 192),
    "ryzen97940hs": ("AMD Ryzen 9 7940HS", 150),
    "ryzen77840hs": ("AMD Ryzen 7 7840HS", 130),
    "ryzen77840u":  ("AMD Ryzen 7 7840U", 110),
    "ryzen57640hs": ("AMD Ryzen 5 7640HS", 102),
    "ryzen57640u":  ("AMD Ryzen 5 7640U", 88),
    "ryzen77735hs": ("AMD Ryzen 7 7735HS", 95),
    "ryzen57535hs": ("AMD Ryzen 5 7535HS", 82),
    "ryzen77730u":  ("AMD Ryzen 7 7730U", 85),
    "ryzen57530u":  ("AMD Ryzen 5 7530U", 76),
    "ryzen57520u":  ("AMD Ryzen 5 7520U", 38),
    "ryzen37320u":  ("AMD Ryzen 3 7320U", 30),

    # AMD Ryzen 6000 Series (Zen 3+)
    "ryzen96900hx": ("AMD Ryzen 9 6900HX", 110),
    "ryzen76800h":  ("AMD Ryzen 7 6800H", 92),
    "ryzen76800u":  ("AMD Ryzen 7 6800U", 85),
    "ryzen56600h":  ("AMD Ryzen 5 6600H", 75),
    "ryzen56600u":  ("AMD Ryzen 5 6600U", 68),

    # AMD Ryzen 5000 Series (Zen 3 / Zen 2)
    "ryzen95900hx": ("AMD Ryzen 9 5900HX", 98),
    "ryzen75800h":  ("AMD Ryzen 7 5800H", 85),
    "ryzen75800u":  ("AMD Ryzen 7 5800U", 78),
    "ryzen55600h":  ("AMD Ryzen 5 5600H", 72),
    "ryzen55600u":  ("AMD Ryzen 5 5600U", 64),
    "ryzen75700u":  ("AMD Ryzen 7 5700U", 62),
    "ryzen55500u":  ("AMD Ryzen 5 5500U", 50),
    "ryzen35300u":  ("AMD Ryzen 3 5300U", 34),

    # AMD Ryzen 4000 Series (Zen 2)
    "ryzen94900h":  ("AMD Ryzen 9 4900H", 80),
    "ryzen74800h":  ("AMD Ryzen 7 4800H", 76),
    "ryzen74700u":  ("AMD Ryzen 7 4700U", 55),
    "ryzen54600h":  ("AMD Ryzen 5 4600H", 58),
    "ryzen54500u":  ("AMD Ryzen 5 4500U", 42),
    "ryzen34300u":  ("AMD Ryzen 3 4300U", 28),

    # Apple M-Series CPUs
    "m4max":   ("Apple M4 Max", 180),
    "m4pro":   ("Apple M4 Pro", 135),
    "m4":      ("Apple M4", 100),
    "m3max":   ("Apple M3 Max", 165),
    "m3pro":   ("Apple M3 Pro", 120),
    "m3":      ("Apple M3", 88),
    "m2max":   ("Apple M2 Max", 140),
    "m2pro":   ("Apple M2 Pro", 115),
    "m2":      ("Apple M2", 80),
    "m1max":   ("Apple M1 Max", 110),
    "m1pro":   ("Apple M1 Pro", 95),
    "m1":      ("Apple M1", 68)
}

# ────────────────────────────────────────────────────────────────────────
# 2. GPU LOOKUP DATABASE (Normalized Key -> (Display Name, Relative Score))
# ────────────────────────────────────────────────────────────────────────
GPU_DATABASE: dict[str, tuple[str, int]] = {
    # NVIDIA GeForce RTX 40 Series Laptop
    "rtx4090": ("GeForce RTX 4090 Laptop", 200),
    "rtx4080": ("GeForce RTX 4080 Laptop", 170),
    "rtx4070": ("GeForce RTX 4070 Laptop", 115),
    "rtx4060": ("GeForce RTX 4060 Laptop", 100),
    "rtx4050": ("GeForce RTX 4050 Laptop", 82),

    # NVIDIA GeForce RTX 30 Series Laptop
    "rtx3080ti": ("GeForce RTX 3080 Ti Laptop", 122),
    "rtx3080":   ("GeForce RTX 3080 Laptop", 110),
    "rtx3070ti": ("GeForce RTX 3070 Ti Laptop", 108),
    "rtx3070":   ("GeForce RTX 3070 Laptop", 98),
    "rtx3060":   ("GeForce RTX 3060 Laptop", 80),
    "rtx3050ti": ("GeForce RTX 3050 Ti Laptop", 54),
    "rtx3050":   ("GeForce RTX 3050 Laptop", 48),

    # NVIDIA GeForce GTX Series Mobile
    "gtx1660ti": ("GeForce GTX 1660 Ti Mobile", 45),
    "gtx1650ti": ("GeForce GTX 1650 Ti Mobile", 35),
    "gtx1650":   ("GeForce GTX 1650 Mobile", 32),

    # AMD Radeon RX Mobile
    "rx6800m":    ("Radeon RX 6800M", 112),
    "rx6700m":    ("Radeon RX 6700M", 92),
    "rx6600m":    ("Radeon RX 6600M", 78),
    "rx7600mxt":  ("Radeon RX 7600M XT", 88),
    "rx7600m":    ("Radeon RX 7600M", 76),
    "rx7600s":    ("Radeon RX 7600S", 74),
    "rx6500m":    ("Radeon RX 6500M", 42),

    # Intel Arc Mobile
    "arca770m": ("Intel Arc A770M", 100),
    "arca730m": ("Intel Arc A730M", 96),
    "arca570m": ("Intel Arc A570M", 68),
    "arca530m": ("Intel Arc A530M", 55),
    "arca370m": ("Intel Arc A370M", 40),
    "arca350m": ("Intel Arc A350M", 30),

    # Intel Integrated Graphics
    "irisxe":           ("Intel Iris Xe Graphics (96EU)", 15),
    "intelirisxe":      ("Intel Iris Xe Graphics (96EU)", 15),
    "uhdgraphics":      ("Intel UHD Graphics", 8),
    "inteluhdgraphics": ("Intel UHD Graphics", 8),
    "hdgraphics":       ("Intel HD Graphics", 5),

    # AMD Radeon Integrated Graphics
    "tradeongraphics":  ("AMD Radeon Integrated Graphics", 18),
    "radeongraphics":   ("AMD Radeon Integrated Graphics", 18),
    "radeon780m":       ("AMD Radeon 780M Graphics", 32),
    "radeon680m":       ("AMD Radeon 680M Graphics", 26),
    "radeon760m":       ("AMD Radeon 760M Graphics", 22),
    "radeon660m":       ("AMD Radeon 660M Graphics", 18),

    # Apple M-Series Integrated GPU Mappings (resolved dynamically via CPU)
    "m4maxgpu":   ("Apple M4 Max GPU (40-core)", 140),
    "m4progpu":   ("Apple M4 Pro GPU (20-core)", 95),
    "m4gpu":      ("Apple M4 GPU (10-core)", 55),
    "m3maxgpu":   ("Apple M3 Max GPU (40-core)", 120),
    "m3progpu":   ("Apple M3 Pro GPU (18-core)", 78),
    "m3gpu":      ("Apple M3 GPU (10-core)", 46),
    "m2maxgpu":   ("Apple M2 Max GPU (38-core)", 105),
    "m2progpu":   ("Apple M2 Pro GPU (19-core)", 72),
    "m2gpu":      ("Apple M2 GPU (10-core)", 40),
    "m1maxgpu":   ("Apple M1 Max GPU (32-core)", 88),
    "m1progpu":   ("Apple M1 Pro GPU (16-core)", 62),
    "m1gpu":      ("Apple M1 GPU (8-core)", 30),
}


# ────────────────────────────────────────────────────────────────────────
# 3. HELPER FUNCTIONS
# ────────────────────────────────────────────────────────────────────────
def normalize_name(text: str) -> str:
    """Strip spaces, punctuation, and Greek tones to normalize keys."""
    if not text:
        return ""
    # Strip tones from characters (e.g. ή -> η)
    nfkd_form = unicodedata.normalize("NFD", text.lower())
    clean_text = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    # Remove all non-alphanumeric chars
    return re.sub(r"[^a-z0-9]", "", clean_text)


def parse_laptop_title(title: str) -> dict:
    """
    Extracts CPU, GPU, RAM capacity (GB), and SSD capacity (GB) from a Skroutz product title.
    Returns:
      {
        "title": raw_title,
        "cpu": {"raw": str, "display": str, "score": int} | None,
        "gpu": {"raw": str, "display": str, "score": int} | None,
        "ram_gb": int | None,
        "ssd_gb": int | None
      }
    """
    # 1. Clean and normalize listing text representation
    title_clean = title.strip()
    
    # 2. Check for parentheses (standard specs list on Skroutz)
    tokens: list[str] = []
    paren_match = re.search(r"\(([^)]+)\)", title_clean)
    if paren_match:
        tokens = [t.strip() for t in paren_match.group(1).split("/") if t.strip()]

    cpu_info: dict | None = None
    gpu_info: dict | None = None

    # 3. Regex Patterns
    # CPU Regex
    pat_intel = re.compile(r"\b(?:intel\s+core\s+)?(i[3579])[- ]*(\d{4,5}[a-z]*|n\d{3})\b", re.I)
    pat_ultra = re.compile(r"\b(?:core\s+)?ultra\s+([579])[- ]*(\d{3}[a-z]*)\b", re.I)
    pat_ryzen = re.compile(r"\bryzen\s+([3579])[- ]*(\d{4}[a-z]*)\b", re.I)
    pat_apple = re.compile(r"\b(m[1-4])\s*(pro|max)?\b", re.I)

    # GPU Regex
    pat_rtx = re.compile(r"\brtx\s*(4090|4080|4070|4060|4050|3080\s*ti|3080|3070\s*ti|3070|3060|3050\s*ti|3050)\b", re.I)
    pat_gtx = re.compile(r"\bgtx\s*(1660\s*ti|1650\s*ti|1650)\b", re.I)
    pat_rx = re.compile(r"\brx\s*(6800m|6700m|6600m|7600m\s*xt|7600m|7600s|6500m)\b", re.I)
    pat_arc = re.compile(r"\barc\s*(a770m|a730m|a570m|a530m|a370m|a350m)\b", re.I)
    pat_iris = re.compile(r"\b(iris\s*xe|iris\s*graphics|intel\s*iris\s*xe|uhd\s*graphics|intel\s*uhd\s*graphics|uhd|intel\s*uhd|hd\s*graphics|intel\s*hd\s*graphics|hd|intel\s*hd)\b", re.I)
    pat_radeon = re.compile(r"\b(radeon\s*graphics|radeon\s*(780m|680m|760m|660m))\b", re.I)

    def extract_cpu(text: str) -> dict | None:
        # Intel Core
        m = pat_intel.search(text)
        if m:
            raw = f"Core {m.group(0).strip()}"
            key = normalize_name(m.group(1) + m.group(2))
            if key in CPU_DATABASE:
                display, score = CPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"Intel Core {m.group(1).upper()}-{m.group(2).upper()}", "score": None}

        # Intel Core Ultra
        m = pat_ultra.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name("coreultra" + m.group(1) + m.group(2))
            if key in CPU_DATABASE:
                display, score = CPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"Intel Core Ultra {m.group(1)} {m.group(2).upper()}", "score": None}

        # AMD Ryzen
        m = pat_ryzen.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name("ryzen" + m.group(1) + m.group(2))
            if key in CPU_DATABASE:
                display, score = CPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"AMD Ryzen {m.group(1)} {m.group(2).upper()}", "score": None}

        # Apple M Series
        m = pat_apple.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name(raw)
            if key in CPU_DATABASE:
                display, score = CPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"Apple {raw}", "score": None}

        return None

    def extract_gpu(text: str) -> dict | None:
        # NVIDIA RTX
        m = pat_rtx.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name("rtx" + m.group(1))
            if key in GPU_DATABASE:
                display, score = GPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"GeForce {raw} Laptop", "score": None}

        # NVIDIA GTX
        m = pat_gtx.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name("gtx" + m.group(1))
            if key in GPU_DATABASE:
                display, score = GPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"GeForce {raw} Mobile", "score": None}

        # AMD Radeon RX
        m = pat_rx.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name("rx" + m.group(1))
            if key in GPU_DATABASE:
                display, score = GPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"Radeon {raw}", "score": None}

        # Intel Arc
        m = pat_arc.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name("arc" + m.group(1))
            if key in GPU_DATABASE:
                display, score = GPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            return {"raw": raw, "display": f"Intel {raw} Mobile", "score": None}

        # Intel Iris Xe / Iris Graphics / UHD / HD Graphics
        m = pat_iris.search(text)
        if m:
            raw = m.group(0).strip()
            raw_lower = raw.lower()
            if "uhd" in raw_lower:
                key = "uhdgraphics"
            elif "hd" in raw_lower:
                key = "hdgraphics"
            else:
                key = "irisxe"
            display, score = GPU_DATABASE[key]
            return {"raw": raw, "display": display, "score": score}

        # AMD Radeon Integrated / Radeon 780M/680M
        m = pat_radeon.search(text)
        if m:
            raw = m.group(0).strip()
            key = normalize_name(raw)
            if key in GPU_DATABASE:
                display, score = GPU_DATABASE[key]
                return {"raw": raw, "display": display, "score": score}
            # Fallback to general Radeon
            display, score = GPU_DATABASE["radeongraphics"]
            return {"raw": raw, "display": display, "score": score}

        return None

    # Step A: Attempt to parse components inside parenthesis tokens first (high confidence)
    for tok in tokens:
        if not cpu_info:
            cpu_info = extract_cpu(tok)
        if not gpu_info:
            gpu_info = extract_gpu(tok)

    # Step B: Run fallback global extraction on full title if missing
    if not cpu_info:
        cpu_info = extract_cpu(title_clean)
    if not gpu_info:
        gpu_info = extract_gpu(title_clean)

    # Step C: Handle Apple Silicon integrated graphics fallback
    if not gpu_info and cpu_info:
        cpu_disp = cpu_info["display"]
        if "Apple M" in cpu_disp:
            soc_key = normalize_name(cpu_disp).replace("apple", "")
            gpu_key = soc_key + "gpu"
            if gpu_key in GPU_DATABASE:
                display, score = GPU_DATABASE[gpu_key]
                gpu_info = {
                    "raw": cpu_info["raw"] + " Integrated GPU",
                    "display": display,
                    "score": score
                }

    # Step D: Parse Memory (RAM) and SSD
    # Extract all capacity matches: pairs of (number, unit) e.g. [("16", "GB"), ("512", "GB")]
    cap_matches = re.findall(r"\b(\d+)\s*(gb|tb)\b", title_clean, re.I)
    ram_candidates: list[int] = []
    ssd_candidates: list[int] = []

    # Find capacity numbers explicitly associated with storage terms: eMMC, SSD, Flash, Storage, Disk
    # e.g., "32GB eMMC", "64 GB SSD", "64GB Flash", "128GB Storage"
    storage_matches = set()
    for m in re.finditer(r"\b(\d+)\s*(?:gb)?\s*(?:emmc|e-mmc|ssd|flash|storage|disk)\b", title_clean, re.I):
        storage_matches.add(int(m.group(1)))

    for val_str, unit in cap_matches:
        val = int(val_str)
        unit = unit.lower()
        if unit == "tb":
            # Any TB metric is immediately an SSD indicator (1TB = 1000GB)
            ssd_candidates.append(val * 1000)
        else:
            # GB metric: classify based on typical notebook thresholds or storage context
            if val in storage_matches:
                ssd_candidates.append(val)
            else:
                if val <= 96:
                    # System RAM range
                    ram_candidates.append(val)
                if val >= 128:
                    # SSD storage range
                    ssd_candidates.append(val)

    # Resolve capacities (taking the maximum of candidates filters out GPU VRAM or duplicates)
    ram_gb = max(ram_candidates) if ram_candidates else None
    ssd_gb = max(ssd_candidates) if ssd_candidates else None

    return {
        "title": title,
        "cpu": cpu_info,
        "gpu": gpu_info,
        "ram_gb": ram_gb,
        "ssd_gb": ssd_gb
    }


def get_integrated_gpu_score(cpu_display: str) -> int:
    """Determine a default integrated GPU score based on CPU display name."""
    disp_lower = cpu_display.lower()
    if "apple" in disp_lower:
        # Default Apple Silicon GPU
        return 30
    elif "ryzen" in disp_lower or "amd" in disp_lower:
        return 18
    elif "intel" in disp_lower or "core" in disp_lower:
        if "i3" in disp_lower or "-n" in disp_lower or "n305" in disp_lower:
            return 8
        return 15
    return 8  # Generic low-end fallback


def get_laptop_scores(title: str) -> dict:
    """Parses laptop title and computes scores, filling defaults for missing components."""
    specs = parse_laptop_title(title)
    cpu_name = specs["cpu"]["display"] if specs["cpu"] else ""
    cpu_score = specs["cpu"]["score"] if (specs["cpu"] and specs["cpu"].get("score")) else 0
    
    gpu_name = specs["gpu"]["display"] if specs["gpu"] else ""
    gpu_score = specs["gpu"]["score"] if (specs["gpu"] and specs["gpu"].get("score")) else None
    
    if gpu_score is None:
        if cpu_name:
            gpu_score = get_integrated_gpu_score(cpu_name)
            if not gpu_name:
                gpu_name = "Integrated Graphics"
        else:
            gpu_score = 8
            if not gpu_name:
                gpu_name = "Integrated Graphics"
            
    combined_score = cpu_score * 0.5 + gpu_score * 0.5
    
    return {
        "cpu_name": cpu_name,
        "cpu_score": cpu_score,
        "gpu_name": gpu_name,
        "gpu_score": gpu_score,
        "ram_gb": specs["ram_gb"] or 0,
        "ssd_gb": specs["ssd_gb"] or 0,
        "combined_score": round(combined_score, 1)
    }



# ────────────────────────────────────────────────────────────────────────
# 4. UNIT TEST SUITE (DEMONSTRATION RUNNER)
# ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json
    
    test_titles = [
        # Standard layout (Parentheses splits)
        'HP Victus 16-r0007nv 16.1" (i7-13700H/16GB/512GB/RTX 4060)',
        'Dell Inspiron 3520 15.6" (i5-1235U/8GB/512GB SSD/Iris Xe Graphics/W11 S)',
        'Asus ROG Zephyrus G14 GA402XV 14" (Ryzen 9 7940HS/16GB/1TB SSD/RTX 4060/W11 Home)',
        'Asus TUF Gaming A15 FA507NU-LP003 15.6" (Ryzen 7 7735HS/16GB/512GB SSD/RTX 4050/No OS)',
        'Lenovo IdeaPad Slim 3 15IAN8 15.6" (Core i3-N305/8GB/256GB SSD/UHD Graphics/W11 Home)',
        'HP 15-fc0004nv 15.6" (Ryzen 3 7320U/8GB/256GB SSD/Radeon Graphics/W11 Home)',
        
        # Apple MacBook Layout (No separate GPU token; resolved via CPU SoC)
        'Apple MacBook Pro 14" (M3 Max 14-Core CPU/30-Core GPU/36GB/1TB SSD/macOS)',
        'Apple MacBook Air 13.6" (M2/8GB/256GB SSD/macOS)',
        'Apple MacBook Pro 16" (M4 Pro/24GB/512GB SSD/macOS)',
        
        # Flat format layout (No specs parentheses)
        'HP Omen 16 Intel Core i9-14900HX 32GB RAM 1TB SSD GeForce RTX 4080',
        
        # Chromebooks and budget storage (eMMC)
        'Lenovo IdeaPad Slim 1 14IGL05 14" (Celeron N4020/4GB/64GB eMMC/Chrome OS)',
        'HP Stream 11-ak0002nv 11.6" (Celeron N4020/4GB/32GB eMMC/Windows 10 Home S)'
    ]

    print("=== LAUNCHING LAPTOP HARDWARE TITLE PARSER TESTS ===\n")
    for title in test_titles:
        res = parse_laptop_title(title)
        scores = get_laptop_scores(title)
        print(f"Title: {res['title']}")
        print(f"  Parsed CPU: {res['cpu']}")
        print(f"  Parsed GPU: {res['gpu']}")
        print(f"  RAM capacity: {res['ram_gb']} GB | SSD capacity: {res['ssd_gb']} GB")
        print(f"  Scores: {scores}")
        print("-" * 75)
