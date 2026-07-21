"""Discord webhook alerting for deals surfaced by the watch loop.

Webhook resolution: DISCORD_WEBHOOK env var first, then a "discord_webhook"
key in config.json (gitignored — copy config.example.json to create it).
Empty webhook → send_discord() silently no-ops; everything else in the
monitor keeps working."""

import json
import os
from datetime import datetime

import requests


def _load_discord_webhook() -> str:
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if hook:
        return hook
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            return str(json.load(fh).get("discord_webhook") or "").strip()
    except (OSError, ValueError):
        return ""


DISCORD_WEBHOOK = _load_discord_webhook()


def send_discord(listing: dict, reason: str, extra_fields: list | None = None) -> None:
    if not DISCORD_WEBHOOK:
        return
    price_str = f"{listing['price']:.2f} €" if listing["price"] else "?"
    fields = [
        {"name": "Price",     "value": price_str,                  "inline": True},
        {"name": "Condition", "value": listing["condition"] or "–", "inline": True},
        {"name": "Deal",      "value": reason,                     "inline": False},
    ]
    if extra_fields:
        fields.extend(extra_fields)
    fields.append({"name": "Link", "value": listing["url"] or "–", "inline": False})
    embed = {
        "title": "🔥 Deal Found!",
        "description": listing["name"],
        "color": 0xFF6B6B,
        "fields": fields,
        "footer": {"text": f"Skroutz Skoop Monitor • {datetime.now().strftime('%H:%M:%S')}"},
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
        if resp.status_code not in (200, 204):
            print(f"  [Discord] {resp.status_code}: {resp.text[:120]}")
    except Exception as e:
        print(f"  [Discord] Send error: {e}")
