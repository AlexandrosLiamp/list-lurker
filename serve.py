"""
serve.py — local dashboard server + negotiator control API.
Run:  python serve.py   → opens http://localhost:8080/dashboard.html

Serves the static dashboard (dashboard.html, negotiations.json, the CSVs, …) AND a small JSON
API so you can drive the negotiator from the browser — the "handle everything through the
website" control center:

  GET  /api/state          -> {negotiations:[...], pending:[...], jobs:[...]}
  POST /api/review         -> start MODE 3 sweep `negotiator.py review`                (bg job)
  POST /api/link    {url}  -> start MODE 1 `negotiator.py link <url> --confirm`        (bg job)
  POST /api/approve {url}  -> send a reviewed proposal `negotiator.py approve …`       (bg job)
  POST /api/reject  {url}  -> drop a proposal from pending_offers.json                 (sync)

Offer-sending runs in a background thread (browser automation is slow); the dashboard polls
/api/state for results. Every send still goes through negotiator.py → negotiator_send (floor
check, guarded submit, anti-spam, ledger) — the API adds no new way to bypass safety.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path

import reports

# The negotiator subsystem (offer agent + seller ledger + message-correction memory)
# is a private extension that ships separately from the public repo. Without it the
# server still serves the dashboard and the data/API endpoints; the negotiator
# endpoints below answer 501 and the dashboard hides its Negotiations tab.
try:
    import corrections
    import ledger
    NEGOTIATOR_AVAILABLE = True
except ImportError:
    corrections = ledger = None
    NEGOTIATOR_AVAILABLE = False

PORT = 8080
ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
URL = f"http://localhost:{PORT}/dashboard.html"
PYTHON = sys.executable

NEG_FILE = "negotiations.json"
PENDING_FILE = "pending_offers.json"
CFG_FILE = "negotiator_config.json"


def _write_cfg(cfg: dict):
    (ROOT / CFG_FILE).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_pending(pend: list):
    (ROOT / PENDING_FILE).write_text(json.dumps(pend, ensure_ascii=False, indent=2), encoding="utf-8")


STRATEGY_FILE = "Negotiation_strategy.md"
_VOICE_START = "<!-- USER-VOICE:START -->"
_VOICE_END = "<!-- USER-VOICE:END -->"


def _read_voice() -> str:
    p = ROOT / STRATEGY_FILE
    if not p.exists():
        return ""
    import re
    m = re.search(re.escape(_VOICE_START) + r"(.*?)" + re.escape(_VOICE_END),
                  p.read_text(encoding="utf-8"), re.S)
    if not m:
        return ""
    body = m.group(1).strip()
    # hide the placeholder italics from the editor
    return "" if body.startswith("_None set yet.") else body


def _write_voice(text: str) -> None:
    import re
    p = ROOT / STRATEGY_FILE
    doc = p.read_text(encoding="utf-8") if p.exists() else ""
    text = (text or "").strip()
    inner = text if text else ("_None set yet. Add rules on the dashboard - e.g. \"always greet "
                               "with γεια σας\", \"never say φιλε\", \"don't sound in a hurry\"._")
    block = f"{_VOICE_START}\n{inner}\n{_VOICE_END}"
    if _VOICE_START in doc:
        doc = re.sub(re.escape(_VOICE_START) + r".*?" + re.escape(_VOICE_END),
                     lambda _m: block, doc, flags=re.S)
    else:
        doc += ("\n\n## VOICE RULES  (set directly by the owner — highest priority for wording)\n\n"
                + block + "\n")
    p.write_text(doc, encoding="utf-8")


def _tmp_msg(text: str) -> str:
    """Write a message to a UTF-8 temp file (so Greek survives the subprocess CLI) and return its path."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="msg_", dir=str(ROOT))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text or "")
    return path

_jobs: dict[str, dict] = {}
_job_procs: dict[str, subprocess.Popen] = {}
_jobs_lock = threading.Lock()


def _read_json(path: str, default):
    p = ROOT / path
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _run_job(job_id: str, args: list[str]):
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["current_step"] = "starting..."
    out_lines = []
    ok = False
    proc = None
    try:
        proc = subprocess.Popen(
            [PYTHON, "-u", "negotiator.py", *args],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1
        )
        with _jobs_lock:
            _job_procs[job_id] = proc
        for line in iter(proc.stdout.readline, ""):
            line_str = line.strip()
            if line_str:
                out_lines.append(line_str)
                with _jobs_lock:
                    _jobs[job_id]["current_step"] = line_str
                    _jobs[job_id]["tail"] = "\n".join(out_lines[-12:])
        proc.stdout.close()
        try:
            returncode = proc.wait(timeout=900)
            ok = returncode == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            out_lines.append("error: timeout expired (900s)")
            ok = False
    except Exception as e:
        out_lines.append(f"error: {e}")
        ok = False
    finally:
        with _jobs_lock:
            _job_procs.pop(job_id, None)
    with _jobs_lock:
        _jobs[job_id].update(status="done", ok=ok, finished=time.time(),
                             current_step="", tail="\n".join(out_lines[-12:]))


def _start_job(label: str, args: list[str]) -> str:
    jid = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[jid] = {"id": jid, "label": label, "cmd": " ".join(args), "status": "queued",
                      "started": time.time(), "ok": None, "tail": "", "current_step": ""}
    threading.Thread(target=_run_job, args=(jid, args), daemon=True).start()
    return jid


def _jobs_snapshot() -> list[dict]:
    with _jobs_lock:
        return sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)[:8]


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _csv_row_count(self, filename: str) -> int:
        p = ROOT / filename
        if not p.exists():
            return 0
        try:
            with open(p, "r", encoding="utf-8") as f:
                return max(0, sum(1 for _ in f) - 1)
        except Exception:
            return 0

    def do_GET(self):
        if self.path.startswith("/api/reports"):
            return self._json({"urls": sorted(reports.reported_urls()),
                               "counts": reports.counts(), "total": reports.total()})
        if self.path.startswith("/api/state"):
            cfg = _read_json(CFG_FILE, {})
            try:
                import archive_store
                live_n, arch_n = archive_store.live_count(), archive_store.archive_count()
            except Exception:
                live_n = arch_n = None
            # A proposal whose listing is already in the ledger has been sent (or recorded) — it
            # must never linger in the pending queue. Prune + persist so the dashboard self-heals.
            if NEGOTIATOR_AVAILABLE:
                pend = _read_json(PENDING_FILE, [])
                kept = [p for p in pend if not ledger.by_url(p.get("url", ""))]
                if len(kept) != len(pend):
                    _write_pending(kept)
                negotiations = ledger.with_messages(ledger.load())
                corr_count = corrections.count()
            else:
                kept, negotiations, corr_count = [], [], 0
            return self._json({"negotiator": NEGOTIATOR_AVAILABLE,
                               "negotiations": negotiations,
                               "pending": kept,
                               "jobs": _jobs_snapshot(),
                               "targets": cfg.get("targets", []),
                               "ai_discovery": cfg.get("ai_discovery", {}),
                               "auto_snipe": bool(cfg.get("auto_snipe", False)),
                               "hide_declined": bool(cfg.get("hide_declined", False)),
                               "live_count": live_n, "archive_count": arch_n,
                               "laptop_count": self._csv_row_count("laptop_retail.csv"),
                               "corrections": corr_count,
                               "voice": _read_voice(),
                               "ai_engine": cfg.get("ai_engine", "claude_cli"),
                               "deepseek_api_key": cfg.get("deepseek_api_key", ""),
                               "deepseek_model": cfg.get("deepseek_model", "deepseek-chat"),
                               "gate": cfg.get("gate", "auto"),
                               "retail_deals": _read_json("retail_deals.json", {})})
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        body = self._body()
        url = (body.get("url") or "").strip()
        path = self.path.split('?')[0]

        # Negotiator-only endpoints answer 501 when the private subsystem is absent.
        NEGOTIATOR_ONLY = {
            "/api/scan", "/api/ai", "/api/poll", "/api/learn", "/api/link_preview",
            "/api/link_send", "/api/approve", "/api/edit_message", "/api/edit_price",
            "/api/teach", "/api/voice", "/api/reject", "/api/note",
            "/api/negotiation_status", "/api/counter_preview", "/api/negotiation_hide",
        }
        if path in NEGOTIATOR_ONLY and not NEGOTIATOR_AVAILABLE:
            return self._json({"error": "negotiator subsystem not installed"}, 501)

        if path == "/api/ai_config":
            cfg = _read_json(CFG_FILE, {})
            if "ai_engine" in body:
                cfg["ai_engine"] = str(body["ai_engine"])
            if "deepseek_api_key" in body:
                cfg["deepseek_api_key"] = str(body["deepseek_api_key"])
            if "deepseek_model" in body:
                cfg["deepseek_model"] = str(body["deepseek_model"])
            _write_cfg(cfg)
            return self._json({"ok": True, "ai_engine": cfg.get("ai_engine"),
                               "deepseek_api_key": cfg.get("deepseek_api_key"),
                               "deepseek_model": cfg.get("deepseek_model", "deepseek-chat")})

        if path == "/api/stop_job":
            jid = body.get("job_id")
            if not jid:
                return self._json({"error": "missing job_id"}, 400)
            with _jobs_lock:
                proc = _job_procs.get(jid)
                if proc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            return self._json({"ok": True})

        # ── jobs that just run a negotiator subcommand ──
        if path == "/api/scan":
            return self._json({"job": _start_job("scan", ["review"])})
        if path == "/api/ai":
            # persist the discovery scope/constraints so mode_ai picks them up
            cfg = _read_json(CFG_FILE, {})
            d = cfg.setdefault("ai_discovery", {})
            if "scope" in body:
                d["scope"] = body["scope"] if body["scope"] in ("targets", "discover", "prompt") else "targets"
            for k, src in (("price_min", "price_min"), ("price_max", "price_max"), ("min_perf", "min_perf")):
                v = body.get(src)
                d[k] = float(v) if v not in (None, "") else None
            if "prompt" in body:
                d["prompt"] = (body.get("prompt") or "").strip()
            _write_cfg(cfg)
            return self._json({"job": _start_job("ai", ["ai"])})
        if path == "/api/poll":
            return self._json({"job": _start_job("poll", ["poll"])})
        if path == "/api/learn":
            return self._json({"job": _start_job("learn", ["learn"])})

        # ── Negotiate-link: synchronous preview, then send ──
        if path == "/api/link_preview":
            if "skroutz.gr/skoop/" not in url:
                return self._json({"error": "not a Skoop listing URL"}, 400)
            try:
                proc = subprocess.run([PYTHON, "negotiator.py", "link", url, "--preview"], cwd=ROOT,
                                      capture_output=True, text=True, encoding="utf-8", timeout=180)
                out = (proc.stdout or "") + (proc.stderr or "")
                line = next((l for l in out.splitlines() if l.startswith("LINK_PREVIEW_JSON ")), None)
                if not line:
                    return self._json({"error": "preview failed", "detail": out[-400:]}, 500)
                return self._json({"ok": True, "preview": json.loads(line[len("LINK_PREVIEW_JSON "):])})
            except Exception as e:
                return self._json({"error": f"preview error: {e}"}, 500)
        if path == "/api/link_send":
            if "skroutz.gr/skoop/" not in url:
                return self._json({"error": "not a Skoop listing URL"}, 400)
            try:
                price = float(body.get("price"))
            except (TypeError, ValueError):
                return self._json({"error": "missing/invalid price"}, 400)
            tmp = _tmp_msg(body.get("message") or "")
            return self._json({"job": _start_job("link send",
                              ["link", url, "--price", str(price), "--message-file", tmp, "--confirm"])})

        # ── pending-proposal actions ──
        if path == "/api/approve":
            if not url:
                return self._json({"error": "missing url"}, 400)
            msg = (body.get("message") or "").strip()
            price = body.get("price")
            pend = _read_json(PENDING_FILE, [])
            for p in pend:
                if p.get("url") == url:
                    if msg and msg != p.get("message"):
                        original = p.get("message_original") or p.get("message", "")
                        p["message_original"] = original
                        p["message"] = msg
                        ledger.set_notes(url, f"[message edited] {msg}")
                        corrections.add(url, original, msg, title=p.get("title"), source="approve")
                    if price not in (None, ""):
                        p["offer"] = float(price)
                    break
            _write_pending(pend)
            args = ["approve", url, "--confirm"]
            if msg:
                args += ["--message-file", _tmp_msg(msg)]
            if price not in (None, ""):
                args += ["--price", str(float(price))]
            return self._json({"job": _start_job("approve", args)})
        if path == "/api/edit_message" or path == "/api/edit_price":
            if not url:
                return self._json({"error": "missing url"}, 400)
            pend = _read_json(PENDING_FILE, [])
            found = False
            taught = False
            for p in pend:
                if p.get("url") == url:
                    found = True
                    if "message" in body:
                        original = p.get("message_original") or p.get("message", "")
                        new_msg = (body.get("message") or "").strip()
                        if "message_original" not in p:
                            p["message_original"] = p.get("message", "")
                        p["message"] = new_msg
                        # learn from the correction even if this offer is never sent
                        if corrections.add(url, original, new_msg, title=p.get("title"), source="edit"):
                            taught = True
                    if body.get("price") not in (None, ""):
                        p["offer"] = float(body["price"])
                    break
            if not found:
                return self._json({"error": "listing not in pending"}, 404)
            _write_pending(pend)
            return self._json({"ok": True, "pending": pend, "taught": taught})
        if path == "/api/teach":
            # capture a message correction directly (e.g. from the link-preview tab), no send needed
            original = (body.get("original") or "").strip()
            corrected = (body.get("corrected") or body.get("message") or "").strip()
            rec = corrections.add(url or "(link)", original, corrected,
                                  title=body.get("title"), source="teach")
            return self._json({"ok": True, "taught": bool(rec), "count": corrections.count()})
        if path == "/api/report":
            rec = reports.add(
                category=body.get("category", "other"), url=url, name=body.get("name", ""),
                price=body.get("price"), reason=body.get("reason", "other"),
                note=body.get("note", ""), source=body.get("source", "table"))
            return self._json({"ok": True, "report": rec, "total": reports.total(),
                               "urls": sorted(reports.reported_urls())})
        if path == "/api/voice":
            # owner's direct do/don't rules for message wording → written into the strategy doc,
            # which is the system prompt for every crafted message.
            _write_voice(body.get("text", ""))
            return self._json({"ok": True, "voice": _read_voice()})
        if path == "/api/reject":
            _write_pending([p for p in _read_json(PENDING_FILE, []) if p.get("url") != url])
            return self._json({"ok": True, "pending": _read_json(PENDING_FILE, [])})
        if path == "/api/note":
            if not url:
                return self._json({"error": "missing url"}, 400)
            ledger.set_notes(url, body.get("notes", ""))
            return self._json({"ok": True})
        if path == "/api/negotiation_status":
            if not url:
                return self._json({"error": "missing url"}, 400)
            status = body.get("status")
            if not status:
                return self._json({"error": "missing status"}, 400)
            rec = ledger.update_status(url, status)
            if not rec:
                return self._json({"error": "negotiation not found"}, 404)
            return self._json({"ok": True, "record": rec})
        if path == "/api/counter_preview":
            if not url:
                return self._json({"error": "missing url"}, 400)
            try:
                cp = float(body.get("counter_price"))
            except (TypeError, ValueError):
                return self._json({"error": "missing/invalid counter_price"}, 400)
            rec = ledger.by_url(url)
            if not rec:
                return self._json({"error": "negotiation not found"}, 404)
            cfg = _read_json(CFG_FILE, {})
            import negotiator
            details = {"title": rec.get("title", ""), "condition": ""}
            msg = negotiator.craft_counter_message(details, cp, cfg)
            return self._json({"ok": True, "message": msg, "suggested_price": cp})
        if path == "/api/negotiation_hide":
            if not url:
                return self._json({"error": "missing url"}, 400)
            hidden = bool(body.get("hidden", True))
            rec = ledger.set_hidden(url, hidden)
            if not rec:
                return self._json({"error": "negotiation not found"}, 404)
            return self._json({"ok": True, "record": rec})

        # ── targets (shared filter: which cards + the price you want) ──
        if path == "/api/targets":
            cfg = _read_json(CFG_FILE, {})
            tl = cfg.setdefault("targets", [])
            action = body.get("action", "set")
            if action == "add":
                model = (body.get("model") or "").strip()
                if not model:
                    return self._json({"error": "missing model"}, 400)
                try:
                    tp = float(body.get("target"))
                except (TypeError, ValueError):
                    return self._json({"error": "missing/invalid target price"}, 400)
                tl = [t for t in tl if t.get("model", "").lower() != model.lower()]  # replace dupes
                tl.append({"model": model, "target_price_eur": tp})
                cfg["targets"] = tl
            elif action == "remove":
                idx = body.get("index")
                if isinstance(idx, int) and 0 <= idx < len(tl):
                    tl.pop(idx)
            elif action == "set" and isinstance(body.get("targets"), list):
                cfg["targets"] = body["targets"]
            _write_cfg(cfg)
            return self._json({"ok": True, "targets": cfg.get("targets", [])})

        # ── toggles ──
        if path == "/api/snipe":
            cfg = _read_json(CFG_FILE, {})
            cfg["auto_snipe"] = bool(body.get("on"))
            _write_cfg(cfg)
            return self._json({"ok": True, "auto_snipe": cfg["auto_snipe"]})
        if path == "/api/hide_declined":
            cfg = _read_json(CFG_FILE, {})
            cfg["hide_declined"] = bool(body.get("on"))
            _write_cfg(cfg)
            return self._json({"ok": True, "hide_declined": cfg["hide_declined"]})

        # ── data: archive-before-purge ──
        if path == "/api/purge":
            try:
                import archive_store
                result = archive_store.archive_and_purge()
                return self._json({"ok": True, **result})
            except Exception as e:
                return self._json({"error": f"purge failed: {e}"}, 500)

        return self._json({"error": "unknown endpoint"}, 404)


def _open_browser():
    time.sleep(0.6)
    webbrowser.open(URL)


def _cleanup_tmp_msgs():
    """Remove leftover temp message files (best-effort) from earlier runs."""
    import glob
    for f in glob.glob(str(ROOT / "msg_*.txt")):
        try:
            os.remove(f)
        except OSError:
            pass


if __name__ == "__main__":
    _cleanup_tmp_msgs()
    print(f"Dashboard + control API: {URL}")
    print("Press Ctrl+C to stop.\n")
    threading.Thread(target=_open_browser, daemon=True).start()
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
