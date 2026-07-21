"""One-shot CSV maintenance commands: interactive purge + dedup.

Both are dispatched early by monitor.py (before the browser spins up) since
they're pure file work. Kept out of monitor.py to keep the entry point a
thin dispatcher; nothing else in the codebase imports these.
"""

import csv
import glob
import os
import re
import shutil
from datetime import datetime as _dt

import applog
from config import (CPU_LOG, CPU_RETAIL_LOG, FB_GPU_LOG, GPU_LOG,
                    GPU_RETAIL_LOG, MOBO_LOG, MOBO_RETAIL_LOG, RAM_LOG,
                    RAM_RETAIL_LOG, VENDORA_GPU_LOG, VINTED_CPU_LOG,
                    VINTED_GPU_LOG, VINTED_MOBO_LOG, VINTED_RAM_LOG)

log = applog.get_logger()


def purge_data() -> None:
    """Interactively delete chosen CSV 'databases' and ALL backup snapshots.
    Pure file work — no browser needed (dispatched early, like dedup_csvs).
    Asks which file(s) to wipe, then asks for confirmation before deleting.
    Per the user's choice, any successful purge also removes every backup_csv_*
    directory, regardless of which files were selected.

    Login sessions & credentials are NEVER purgeable — not by number, not by "ALL".
    These must be managed separately:
      - fb_state.json       → `python fb_marketplace.py --login`
      - skroutz_state.json  → `python recon_skroutz_offer.py --login`
      - email_config.json   → copy from email_config.example.json & fill in"""

    # ── deny-list: files purge must NEVER touch ──────────────────────────
    _PURGE_NEVER = {"fb_state.json", "skroutz_state.json", "email_config.json"}

    # CSV "databases" are throwaway: a crawl rebuilds them.
    csv_targets = [GPU_LOG, RAM_LOG, CPU_LOG, MOBO_LOG,
                   VENDORA_GPU_LOG, FB_GPU_LOG,
                   VINTED_GPU_LOG, VINTED_CPU_LOG, VINTED_RAM_LOG, VINTED_MOBO_LOG,
                   GPU_RETAIL_LOG, RAM_RETAIL_LOG, CPU_RETAIL_LOG, MOBO_RETAIL_LOG]
    SESSION_FILE = "fb_state.json"
    targets = csv_targets + [SESSION_FILE]   # selectable by number, but not swept up by "ALL"

    print("Which database(s) to purge?\n")
    for i, f in enumerate(targets, 1):
        tag = "" if os.path.isfile(f) else "  (missing)"
        note = ("   ← FB login session, NOT auto-rebuilt (needs `fb_marketplace.py --login`)"
                if f == SESSION_FILE else "")
        print(f"  {i:2}) {f}{tag}{note}")
    print("   a) ALL databases (CSVs only — NOT login sessions or credentials)")
    sel = input("\n> ").strip().lower()

    if sel in ("a", "all"):
        chosen = list(csv_targets)           # bulk purge = throwaway CSVs only, never sessions/creds
    else:
        chosen = []
        for tok in re.split(r"[,\s]+", sel):
            if not tok:
                continue
            if tok.isdigit() and 1 <= int(tok) <= len(targets):
                f = targets[int(tok) - 1]
                if f not in chosen:
                    chosen.append(f)
            else:
                print(f"Ignoring unrecognised selection '{tok}'.")
    if not chosen:
        print("Nothing selected. Aborted.")
        return

    # ── hard guard: refuse to delete any deny-listed file ──────────────
    blocked = [f for f in chosen if f in _PURGE_NEVER]
    if blocked:
        print(f"\n⚠ REFUSED: these files are login sessions / credentials and will NOT be deleted:")
        for f in blocked:
            print(f"    {f}")
        chosen = [f for f in chosen if f not in _PURGE_NEVER]
        if not chosen:
            print("Nothing left to purge. Aborted.")
            return

    backups = sorted(glob.glob("backup_csv_*"))
    print("\nThis will PERMANENTLY DELETE:")
    for f in chosen:
        print(f"  - {f}" + ("" if os.path.isfile(f) else "  (missing)"))
    if SESSION_FILE in chosen:
        print(f"  ⚠ {SESSION_FILE} is your FB login session — deleting it logs the scraper "
              f"out; restore with `python fb_marketplace.py --login`.")
    if backups:
        print(f"  - ALL backup snapshots ({len(backups)} dir): " + ", ".join(backups))
    confirm = input('\nType "yes" to confirm: ').strip().lower()
    if confirm != "yes":
        print("Aborted. Nothing was deleted.")
        return

    for f in chosen:
        try:
            os.remove(f)
            print(f"  deleted {f}")
        except FileNotFoundError:
            print(f"  (already gone) {f}")
        except OSError as e:
            print(f"  FAILED {f}: {e}")
    for d in backups:
        shutil.rmtree(d, ignore_errors=True)
        print(f"  removed {d}/")
    log.info("purge: deleted %s + %d backup dir(s)", ", ".join(chosen), len(backups))
    print("Purge complete.")


def dedup_csvs() -> None:
    """One-shot cleanup of existing duplicate rows. Keeps one row per (url, price)
    — collapses identical repeats while preserving genuine price changes. Backs up first."""
    bdir = "backup_csv_" + _dt.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(bdir, exist_ok=True)
    before = after = 0
    for f in (RAM_LOG, GPU_LOG, CPU_LOG, MOBO_LOG):
        if not os.path.isfile(f):
            continue
        with open(f, encoding="utf-8") as fh:
            rdr = csv.DictReader(fh); fields = rdr.fieldnames; rows = list(rdr)
        shutil.copy(f, os.path.join(bdir, f))
        seen, kept = set(), []
        for r in rows:
            url = (r.get("url") or "").strip()
            if not url:
                kept.append(r); continue
            key = (url, str(r.get("price") or "").strip())
            if key in seen:
                continue
            seen.add(key); kept.append(r)
        with open(f, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(kept)
        print(f"  {f:18} {len(rows):>5} -> {len(kept):>5}  ({len(rows)-len(kept)} duplicate rows removed)")
        before += len(rows); after += len(kept)
    print(f"\nTotal: {before} -> {after} rows ({before-after} removed). Backup: {bdir}")
