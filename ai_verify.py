"""
AI-powered deal verification — uses your local Claude subscription via the
Claude Code CLI (`claude -p`), NOT a billed API key.

Pipeline:  find a deal  →  crawl the actual listing page  →  extract its text  →
pipe it to `claude -p` (print/headless mode, authenticated by your subscription)
→  Claude returns cleaned, structured JSON (is it a multi-item post? which items
are still available? corrected name/price/condition per item)  →  caller fixes
the dataset (drop sold, split multi-item, correct price).

Requires the Claude Code CLI to be installed and logged in (`claude` on PATH,
`claude login` done once). If `claude` isn't found, verification is skipped and
the monitor runs exactly as before.
"""

import json
import re
import shutil
import subprocess
import time

def load_config() -> dict:
    from pathlib import Path
    p = Path(__file__).resolve().parent / "negotiator_config.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def get_ai_choice() -> tuple[str, str]:
    cfg = load_config()
    engine = cfg.get("ai_engine", "claude_cli")
    api_key = cfg.get("deepseek_api_key", "").strip()
    if engine == "deepseek_api" and not api_key:
        return "claude_cli", ""
    return engine, api_key

def get_deepseek_model() -> str:
    cfg = load_config()
    return cfg.get("deepseek_model", "deepseek-chat").strip() or "deepseek-chat"

def _run_deepseek_api(system: str, user: str, stdin_text: str, api_key: str, model: str = None) -> dict | None:
    if not model:
        model = get_deepseek_model()
    import urllib.request
    import urllib.error
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{user}\n\n{stdin_text}".strip()}
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"}
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            content = res_data["choices"][0]["message"]["content"]
            return _extract_json(content)
    except Exception as e:
        print(f"    [ai_verify] DeepSeek API error: {str(e)[:120]}", flush=True)
        return None

def _run_deepseek_text(system: str, user: str, stdin_text: str, api_key: str, model: str = None) -> str | None:
    if not model:
        model = get_deepseek_model()
    import urllib.request
    import urllib.error
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{user}\n\n{stdin_text}".strip()}
        ],
        "temperature": 0.5
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            content = res_data["choices"][0]["message"]["content"]
            return content
    except Exception as e:
        print(f"    [ai_verify] DeepSeek API text error: {str(e)[:120]}", flush=True)
        return None

MODEL = "sonnet"            # which subscription model the CLI should use
_CLAUDE_BIN = None


# ── Lightweight result objects (no external deps) ─────────────────────────────
class CleanItem:
    def __init__(self, name, price, condition, available, category="unknown"):
        self.name = name
        self.price = price
        self.condition = condition
        self.available = available
        # category ∈ gpu_card | laptop | prebuilt_pc | mobile_gpu | accessory |
        #            other_component | unknown   (Layer-2 listing-type classification)
        self.category = category

class PostAnalysis:
    def __init__(self, overall_available, is_multi_item, items, notes=""):
        self.overall_available = overall_available
        self.is_multi_item = is_multi_item
        self.items = items
        self.notes = notes


_SYSTEM = (
    "You are a strict JSON extraction endpoint, NOT a chat assistant. "
    "You reply with exactly one minified JSON object and nothing else — no prose, no explanation, "
    "no markdown, no code fences, no extra keys beyond the schema the user gives you. "
    "Your entire response must start with '{' and end with '}'."
)

_USER = (
    "Task: from the second-hand PC-parts listing text on stdin (Greek/English; skroutz.gr/skoop, "
    "insomnia.gr, vendora.gr, facebook marketplace or vinted), report availability, per-item pricing, "
    "AND what each item actually IS — NOT full specs.\n\n"
    "Reply with ONE JSON object using EXACTLY these four top-level keys (no others: no title, specs, "
    "seller, etc.):\n"
    '  overall_available (bool), is_multi_item (bool), items (array), notes (string)\n'
    "Each item has EXACTLY: name (string), price (number or null), "
    'condition ("new"|"like-new"|"used"|"broken"|"unknown"), available (bool), '
    'category (one of "gpu_card"|"laptop"|"prebuilt_pc"|"mobile_gpu"|"accessory"|"other_component"|"unknown").\n\n'
    "CATEGORY is critical. Use:\n"
    "  gpu_card        = a standalone DESKTOP graphics card sold by itself.\n"
    "  laptop          = a laptop/notebook (even if it contains a strong GPU).\n"
    "  prebuilt_pc     = a complete/assembled desktop PC, 'gaming pc', or a bundle of multiple parts.\n"
    "  mobile_gpu      = a laptop/MXM/mobile GPU module (not a desktop card).\n"
    "  accessory       = waterblock, backplate, bracket, riser, cooler, cables, box-only — NOT the card.\n"
    "  other_component = a RAM/CPU/motherboard/PSU/etc. that is not a GPU.\n"
    "A graphics card BUILT INTO a laptop or prebuilt PC is NOT gpu_card — classify the whole item as "
    "laptop or prebuilt_pc, and set its price to the WHOLE system's price.\n\n"
    "EXAMPLE — a real standalone card, available:\n"
    '{"overall_available":true,"is_multi_item":false,"items":[{"name":"Gigabyte RTX 4070 Gaming OC 12GB",'
    '"price":380,"condition":"used","available":true,"category":"gpu_card"}],"notes":""}\n\n'
    "EXAMPLE — a gaming laptop that mentions an RTX 3080 (NOT a GPU deal):\n"
    '{"overall_available":true,"is_multi_item":false,"items":[{"name":"Razer Blade 15 Gaming Laptop",'
    '"price":900,"condition":"used","available":true,"category":"laptop"}],"notes":"laptop, not a standalone GPU"}\n\n'
    "EXAMPLE — a sold single RAM kit:\n"
    '{"overall_available":false,"is_multi_item":false,"items":[{"name":"G.Skill Trident Z 32GB DDR4-3600",'
    '"price":110,"condition":"used","available":false,"category":"other_component"}],"notes":"disabled Πωλήθηκε → sold"}\n\n'
    "Rules: overall_available=false if the whole post is sold/closed (disabled \"Πωλήθηκε\" button, or "
    "πωλήθηκε/δόθηκε/ΤΕΛΟΣ/κρατημένη). is_multi_item=true if >1 distinct product. price is per-item euros "
    "(null if unknown). Ignore site chrome/cookies/nav/'similar products'. Do not invent data.\n"
    "Output the JSON object only, in the exact shape of the examples."
)


def _find_claude():
    global _CLAUDE_BIN
    if _CLAUDE_BIN is None:
        _CLAUDE_BIN = shutil.which("claude") or shutil.which("claude.cmd") or ""
    return _CLAUDE_BIN


def get_client():
    """Return a truthy marker if the chosen AI engine is configured/available, else None."""
    engine, api_key = get_ai_choice()
    if engine == "deepseek_api":
        return "deepseek" if api_key else None
    return "cli" if _find_claude() else None


def extract_post_text(page, url: str, timeout: int = 40000) -> str | None:
    """Navigate to the listing and return its visible text (capped), or None on failure.
    Uses full body text so availability markers (the disabled 'Πωλήθηκε' button on skoop,
    sold/reserved notes in insomnia posts) and the full description are always captured."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception:
        return None
    # JS-heavy sites need longer to paint the listing body before we read it.
    time.sleep(3.0 if any(s in url for s in ("insomnia.gr", "facebook.com", "vinted.")) else 0.8)

    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    body = ""
    try:
        body = page.inner_text("body") or ""
    except Exception:
        pass
    # explicitly include button labels (the sold marker is a disabled button on skoop)
    btn = []
    try:
        for b in page.query_selector_all("button"):
            t = (b.inner_text() or "").strip()
            if t:
                btn.append(t)
    except Exception:
        pass

    body = re.sub(r"\n\s*\n+", "\n", body).strip()      # collapse blank lines to fit more signal
    blob = (f"PAGE TITLE: {title}\nBUTTONS: {' | '.join(dict.fromkeys(btn))}\n\n{body}").strip()
    return blob[:8000] if len(blob) > 20 else None


_SOLD_WORDS = ["sold", "πωλη", "unavailable", "not available", "reserved", "κρατ",
               "δεσμ", "gone", "δοθ", "τελος", "withdrawn", "closed"]

def _coerce_avail(v, default=True):
    """Interpret a bool/string availability value."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.lower()
        if any(k in s for k in _SOLD_WORDS):
            return False
        if "avail" in s or "διαθ" in s or "active" in s:
            return True
    return default

def _coerce_price(v):
    try:
        return float(v) if v is not None and v != "" else None
    except Exception:
        m = re.search(r"\d+(?:\.\d+)?", str(v))
        return float(m.group(0)) if m else None

def _norm_item(d: dict, fallback_name=""):
    name = (d.get("name") or d.get("item") or d.get("title") or d.get("product")
            or d.get("model") or fallback_name)
    cond = str(d.get("condition") or d.get("κατάσταση") or "unknown")
    avail = d.get("available", d.get("availability", d.get("status")))
    category = str(d.get("category") or d.get("type") or "unknown").strip().lower()
    # The model is loose on the price key name: price / price_eur / asking_price / ...
    price = next((d[k] for k in ("price", "price_eur", "price_euro", "asking_price",
                                 "asking_price_eur", "item_price_eur") if d.get(k) is not None), None)
    return CleanItem(str(name).strip(), _coerce_price(price), cond,
                     _coerce_avail(avail, default=True), category=category)

def _to_analysis(data: dict):
    """Tolerantly coerce whatever availability-shaped JSON the model returned into PostAnalysis.
    Returns None if there's no usable availability/item information."""
    # The model is loose on the array key name: items / listings / products / ads / results.
    arr = (data.get("items") or data.get("listings") or data.get("products")
           or data.get("ads") or data.get("results"))
    items = []
    if isinstance(arr, list):
        items = [_norm_item(x) for x in arr if isinstance(x, dict)]
    elif any(k in data for k in ("item", "name", "title", "product")):
        items = [_norm_item(data)]                       # whole object is a single item

    has_overall = any(k in data for k in ("overall_available", "availability", "available", "status"))
    if not items and not has_overall:
        return None                                      # nothing availability-shaped → reject

    if "overall_available" in data:
        overall = _coerce_avail(data["overall_available"])
    elif has_overall:
        overall = _coerce_avail(data.get("availability", data.get("available", data.get("status"))))
    else:
        overall = any(it.available for it in items) if items else True

    multi = data.get("is_multi_item")
    if not isinstance(multi, bool):
        multi = len(items) > 1
    notes = str(data.get("notes") or data.get("note") or "")
    return PostAnalysis(overall, multi, items, notes)


def _extract_json(text: str):
    """Pull the first {...} JSON object out of the model's reply."""
    text = text.strip()
    # strip ``` fences if present
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def analyze_post(text: str, listing_name: str, kind: str) -> "PostAnalysis | None":
    """Analyze the post using the chosen AI engine and parse the structured reply."""
    engine, api_key = get_ai_choice()
    user_prompt = f"{_USER}\n\n(Our scraper labelled this a {kind} listing named {listing_name!r}.)"
    
    if engine == "deepseek_api":
        data = _run_deepseek_api(_SYSTEM, user_prompt, f"LISTING PAGE TEXT:\n{text}", api_key)
        if not isinstance(data, dict):
            return None
        analysis = _to_analysis(data)
        if analysis is None:
            print(f"    [ai_verify] reply not availability-shaped: {data}", flush=True)
        return analysis

    claude = _find_claude()
    if not claude:
        return None
    user = user_prompt
    try:
        proc = subprocess.run(
            [claude, "--model", MODEL, "--output-format", "json",
             "--system-prompt", _SYSTEM, "--exclude-dynamic-system-prompt-sections",
             "-p", user],
            input=f"LISTING PAGE TEXT:\n{text}",
            capture_output=True, text=True, encoding="utf-8",
            timeout=150,
        )
    except Exception as e:
        print(f"    [ai_verify] CLI error: {str(e)[:120]}", flush=True)
        return None
    if proc.returncode != 0:
        print(f"    [ai_verify] CLI exit {proc.returncode}: {(proc.stderr or '')[:120]}", flush=True)
        return None

    # --output-format json wraps the reply: {"type":"result","result":"<text>", ...}
    raw = proc.stdout.strip()
    reply = raw
    try:
        wrapper = json.loads(raw)
        if isinstance(wrapper, dict) and "result" in wrapper:
            reply = wrapper["result"]
    except Exception:
        pass

    data = _extract_json(reply)
    if not isinstance(data, dict):
        print(f"    [ai_verify] no JSON in reply: {reply[:100]!r}", flush=True)
        return None
    analysis = _to_analysis(data)
    if analysis is None:
        print(f"    [ai_verify] reply not availability-shaped: {reply[:100]!r}", flush=True)
    return analysis


# Categories the AI uses for things that are NOT a standalone desktop GPU card.
_NONCARD_CATS = {"laptop", "prebuilt_pc", "prebuilt", "prebuilt pc", "mobile_gpu",
                 "mobile", "mxm", "accessory", "other_component", "other", "ram",
                 "cpu", "motherboard", "mobo", "psu", "monitor", "console"}
# Substrings that mark a non-card even in free-text categories (e.g. "gaming laptop").
# Deliberately excludes "desktop"/"pc"/"system" — a real card IS a desktop PCIe card.
_NONCARD_TOKENS = ("laptop", "notebook", "prebuilt", "mobile", "mxm", "accessory",
                   "waterblock", "water block", "backplate", "bracket", "bundle")

def is_gpu_card_category(cat) -> bool:
    """True if the AI's category is compatible with a standalone desktop GPU. Used only
    as a fallback when the model didn't give an explicit is_gpu_card boolean. A positive
    'graphics card' phrase wins; otherwise laptop/prebuilt/pc/accessory phrases → False."""
    c = str(cat or "").strip().lower()
    if not c or c == "unknown":
        return True                                        # defer to Layer 1
    # Positive: explicitly a graphics card.
    if any(p in c for p in ("graphics card", "graphics_card", "video card", "videocard",
                            "gpu_card", "gpu card", "vga")) and "mobile" not in c:
        return True
    if c in ("gpu", "card", "graphics", "graphics-card", "videocard"):
        return True
    # Negative: explicit non-card category or a system/pc/laptop phrase.
    if c in _NONCARD_CATS:
        return False
    if any(tok in c for tok in _NONCARD_TOKENS):
        return False
    if any(tok in c for tok in ("gaming pc", "desktop pc", "complete pc", "complete_desktop",
                                "gaming_pc", "desktop_pc", "_pc", " pc", "computer",
                                "tower", "συστημα", "build")):
        return False
    return True


# ── Layer-2 GPU verdict (tight schema → reliable model adherence) ──────────────
_GPU_USER = (
    "You are given the visible text of ONE second-hand marketplace listing flagged as a "
    "possible graphics-card deal. Decide what the item FOR SALE actually is.\n\n"
    "Output EXACTLY one minified JSON object with EXACTLY these keys and nothing else:\n"
    '{"is_gpu_card":true,"category":"...","available":true,"price":null}\n\n'
    "is_gpu_card = true ONLY if the item being sold is a STANDALONE DESKTOP graphics card "
    "on its own. Set it FALSE for: a laptop/notebook (even with a great GPU inside); a "
    "complete/prebuilt desktop PC, 'gaming pc' or a bundle of multiple parts; a mobile/MXM "
    "GPU; or an accessory (waterblock, backplate, bracket, cooler, cables, box-only).\n"
    "category = one of gpu_card | laptop | prebuilt_pc | mobile_gpu | accessory | other "
    "(your reason for the is_gpu_card flag).\n"
    "available = false if sold/closed/reserved (πωλήθηκε/δόθηκε/κρατημένο/sold/reserved), else true.\n"
    "price = asking price in EUROS for the item (a number; null if only a deposit is stated, or unknown).\n\n"
    "Examples:\n"
    '  standalone Gigabyte RTX 4070 card 380€  -> {"is_gpu_card":true,"category":"gpu_card","available":true,"price":380}\n'
    '  Razer Blade 15 laptop with RTX 3080     -> {"is_gpu_card":false,"category":"laptop","available":true,"price":900}\n'
    '  Gaming PC Ryzen 5 / RTX 3060 / 16GB     -> {"is_gpu_card":false,"category":"prebuilt_pc","available":true,"price":650}\n'
    '  EK water block for RTX 2080 Ti 180€     -> {"is_gpu_card":false,"category":"accessory","available":true,"price":180}\n'
    "Start your reply with { and end with }. No prose, no extra keys."
)


class GpuVerdict:
    def __init__(self, is_card, category, available, price, notes=""):
        self.is_card = is_card          # standalone desktop GPU card?
        self.category = category
        self.available = available
        self.price = price
        self.notes = notes


def _run_claude_json(system: str, user: str, stdin_text: str):
    """Run `claude -p` with the given prompts + stdin; return the parsed JSON dict or None."""
    claude = _find_claude()
    if not claude:
        return None
    try:
        proc = subprocess.run(
            [claude, "--model", MODEL, "--output-format", "json",
             "--system-prompt", system, "--exclude-dynamic-system-prompt-sections",
             "-p", user],
            input=stdin_text, capture_output=True, text=True, encoding="utf-8", timeout=150)
    except Exception as e:
        print(f"    [ai_verify] CLI error: {str(e)[:120]}", flush=True)
        return None
    if proc.returncode != 0:
        print(f"    [ai_verify] CLI exit {proc.returncode}: {(proc.stderr or '')[:120]}", flush=True)
        return None
    raw = proc.stdout.strip()
    reply = raw
    try:
        wrapper = json.loads(raw)
        if isinstance(wrapper, dict) and "result" in wrapper:
            reply = wrapper["result"]
    except Exception:
        pass
    return _extract_json(reply)


def verify_gpu_card(page, url: str, listing_name: str = "") -> "GpuVerdict | None":
    """Layer-2 GPU gate: open the listing and ask the model what the item really is.
    Returns a GpuVerdict, or None when the engine is unavailable / the page failed / no JSON
    (caller then falls back to Layer 1, which already vetted obvious cards)."""
    engine, api_key = get_ai_choice()
    if engine == "deepseek_api":
        if not url or not api_key:
            return None
    else:
        if not url or not _find_claude():
            return None
            
    text = extract_post_text(page, url)
    if not text:
        return None
        
    if engine == "deepseek_api":
        data = _run_deepseek_api(_SYSTEM, _GPU_USER, f"LISTING PAGE TEXT:\n{text}", api_key)
    else:
        data = _run_claude_json(_SYSTEM, _GPU_USER, f"LISTING PAGE TEXT:\n{text}")
        
    if not isinstance(data, dict):
        return None

    # The model is reliable on MEANING but loose on key NAMES (it'll say "item_type"
    # / "is_standalone_gpu" instead of "category"). Parse tolerantly.
    def _get(*keys):
        for k in keys:
            if k in data and data[k] is not None:
                return data[k]
        return None

    cat = str(_get("category", "item_type", "type", "classification") or "unknown").strip().lower()
    flag = _get("is_standalone_gpu", "is_gpu_card", "is_card", "standalone_gpu", "standalone")
    if isinstance(flag, bool):
        is_card = flag                                   # the model's explicit verdict (best)
    elif isinstance(flag, str):
        is_card = flag.strip().lower() in ("true", "yes", "1")
    else:
        is_card = is_gpu_card_category(cat)              # derive from category
    avail = _coerce_avail(_get("available", "availability", "status", "in_stock"), default=True)
    # asking price only — NOT a deposit/reservation amount.
    price = _coerce_price(_get("price", "asking_price_eur", "asking_price", "price_eur",
                               "item_price_eur"))
    notes = str(_get("notes", "verdict", "note") or "")
    return GpuVerdict(is_card, cat, avail, price, notes)


def verify_listing(page, url: str, listing_name: str, kind: str, client=None) -> "PostAnalysis | None":
    """Full pipeline for one listing: crawl → extract → analyze. None on any failure."""
    engine, api_key = get_ai_choice()
    if engine == "deepseek_api":
        if not url or not api_key:
            return None
    else:
        if not url or not _find_claude():
            return None
            
    text = extract_post_text(page, url)
    if not text:
        return None
    return analyze_post(text, listing_name, kind)
