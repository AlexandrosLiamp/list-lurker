"""Sold-price-archive: capture skoop sold prices instead of dropping them.

Covers the record_sold + sold_path_for helpers directly, and one constructed-data
smoke test per capture point (badge_feed via extract_listings, badge_page via
verify_sold's filter step, ai_verify via verify.py's read-then-archive step)."""

import csv

import archive_store


# ── sold_path_for ─────────────────────────────────────────────────────────────


def test_sold_path_for_gpu_prices():
    assert archive_store.sold_path_for("gpu_prices.csv") == "gpu_sold.csv"


def test_sold_path_for_ram_cpu_mobo():
    assert archive_store.sold_path_for("ram_prices.csv") == "ram_sold.csv"
    assert archive_store.sold_path_for("cpu_prices.csv") == "cpu_sold.csv"
    assert archive_store.sold_path_for("mobo_prices.csv") == "mobo_sold.csv"


def test_sold_path_for_bare_csv():
    """A file without the `_prices` suffix still becomes *_sold.csv."""
    assert archive_store.sold_path_for("vendora_gpu.csv") == "vendora_gpu_sold.csv"


# ── record_sold ───────────────────────────────────────────────────────────────


def _sold_row(url, price=100.0, ts="2026-07-22 10:00:00", via="badge_feed"):
    return {"timestamp": ts, "name": "RTX 5070", "condition": "Μεταχειρισμένο",
            "price": price, "url": url, "detected_via": via}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_record_sold_creates_file_with_header(tmp_path):
    sold_path = tmp_path / "gpu_sold.csv"
    added = archive_store.record_sold([_sold_row("https://x/a")], str(sold_path))
    assert added == 1
    rows = _read(sold_path)
    assert rows[0]["url"] == "https://x/a"
    assert rows[0]["detected_via"] == "badge_feed"
    assert rows[0]["price"] == "100.0"


def test_record_sold_dedups_by_url_across_calls(tmp_path):
    sold_path = tmp_path / "gpu_sold.csv"
    archive_store.record_sold([_sold_row("https://x/a", price=100.0)], str(sold_path))
    added = archive_store.record_sold([_sold_row("https://x/a", price=90.0)], str(sold_path))
    assert added == 0
    rows = _read(sold_path)
    assert len(rows) == 1
    assert rows[0]["price"] == "100.0"   # first-write wins, later duplicate ignored


def test_record_sold_dedups_within_batch(tmp_path):
    sold_path = tmp_path / "gpu_sold.csv"
    rows_in = [_sold_row("https://x/a"), _sold_row("https://x/a", price=90.0),
               _sold_row("https://x/b")]
    added = archive_store.record_sold(rows_in, str(sold_path))
    assert added == 2
    urls = {r["url"] for r in _read(sold_path)}
    assert urls == {"https://x/a", "https://x/b"}


def test_record_sold_fills_missing_timestamp(tmp_path):
    """extract_listings-shaped rows arrive without a timestamp; record_sold fills it."""
    sold_path = tmp_path / "gpu_sold.csv"
    row = {"name": "RTX 5070", "condition": "Μεταχειρισμένο", "price": 100.0,
           "url": "https://x/a", "detected_via": "badge_feed"}
    archive_store.record_sold([row], str(sold_path))
    got = _read(sold_path)[0]
    assert got["timestamp"] != ""


def test_record_sold_skips_rows_without_url(tmp_path):
    sold_path = tmp_path / "gpu_sold.csv"
    added = archive_store.record_sold([_sold_row("")], str(sold_path))
    assert added == 0
    assert not sold_path.exists()


def test_record_sold_empty_input_noop(tmp_path):
    sold_path = tmp_path / "gpu_sold.csv"
    assert archive_store.record_sold([], str(sold_path)) == 0
    assert not sold_path.exists()


def test_record_sold_within_batch_last_wins(tmp_path):
    """A URL's price-history trail (multiple live rows for the same URL, older first)
    must archive the LATEST row — the clearing price — not the oldest. Regression guard
    for the sold-archive stale-price bug."""
    sold_path = tmp_path / "gpu_sold.csv"
    rows_in = [
        _sold_row("https://x/a", price=130.0, ts="2026-07-20 10:00:00"),
        _sold_row("https://x/a", price=105.0, ts="2026-07-22 10:00:00"),
    ]
    archive_store.record_sold(rows_in, str(sold_path))
    got = _read(sold_path)
    assert len(got) == 1
    assert got[0]["price"] == "105.0"                      # last row's price wins
    assert got[0]["timestamp"] == "2026-07-22 10:00:00"    # last row's timestamp too


def test_record_sold_preserves_zero_price(tmp_path):
    """`r.get(k) or fb` would coerce falsy 0.0 to blank; the writer must preserve it."""
    sold_path = tmp_path / "gpu_sold.csv"
    archive_store.record_sold([_sold_row("https://x/a", price=0.0)], str(sold_path))
    got = _read(sold_path)[0]
    assert got["price"] == "0.0"


# ── sold_path_for idempotency ─────────────────────────────────────────────────


def test_sold_path_for_idempotent_on_sold_path():
    """Calling sold_path_for on an already-sold path must not double-suffix it."""
    assert archive_store.sold_path_for("gpu_sold.csv") == "gpu_sold.csv"
    assert archive_store.sold_path_for("ram_sold.csv") == "ram_sold.csv"


# ── record_sold_tagged (the service function all capture points route through) ─


def test_record_sold_tagged_tags_and_writes(tmp_path):
    log_file = str(tmp_path / "gpu_prices.csv")
    row = {"name": "RTX 5070", "condition": "Used", "price": 100.0, "url": "https://x/a"}
    n = archive_store.record_sold_tagged([row], log_file, "badge_feed")
    assert n == 1
    got = _read(archive_store.sold_path_for(log_file))[0]
    assert got["detected_via"] == "badge_feed"
    assert got["url"] == "https://x/a"


def test_record_sold_tagged_empty_rows_returns_zero(tmp_path):
    log_file = str(tmp_path / "gpu_prices.csv")
    assert archive_store.record_sold_tagged([], log_file, "badge_feed") == 0
    assert not (tmp_path / "gpu_sold.csv").exists()


def test_record_sold_tagged_empty_log_file_returns_zero(tmp_path):
    row = {"name": "x", "url": "https://x/a", "price": 1.0}
    assert archive_store.record_sold_tagged([row], "", "badge_feed") == 0
    assert archive_store.record_sold_tagged([row], None, "badge_feed") == 0  # type: ignore[arg-type]


def test_record_sold_tagged_swallows_write_errors(tmp_path, capsys):
    """A failing sold-archive must not crash the calling crawl/verify pass."""
    row = {"name": "x", "url": "https://x/a", "price": 1.0}
    # Pointing log_file at a directory forces the CSV open() to fail on write.
    n = archive_store.record_sold_tagged([row], str(tmp_path / "no" / "such" / "dir.csv"),
                                        "badge_feed")
    assert n == 0
    out = capsys.readouterr().out
    assert "[sold-archive] skipped" in out


def test_record_sold_tagged_overrides_existing_detected_via(tmp_path):
    """Any pre-existing detected_via on the row is replaced by the caller's label."""
    log_file = str(tmp_path / "gpu_prices.csv")
    row = {"name": "x", "url": "https://x/a", "price": 1.0, "detected_via": "wrong"}
    archive_store.record_sold_tagged([row], log_file, "badge_page")
    got = _read(archive_store.sold_path_for(log_file))[0]
    assert got["detected_via"] == "badge_page"


# ── sold_stats ────────────────────────────────────────────────────────────────


def test_sold_stats_missing_file_is_empty(tmp_path):
    got = archive_store.sold_stats(str(tmp_path / "nope_sold.csv"))
    assert got == {"count": 0, "median": None, "min": None, "max": None}


def test_sold_stats_single_row(tmp_path):
    sold_path = tmp_path / "gpu_sold.csv"
    archive_store.record_sold([_sold_row("https://x/a", price=100.0)], str(sold_path))
    got = archive_store.sold_stats(str(sold_path))
    assert got == {"count": 1, "median": 100.0, "min": 100.0, "max": 100.0}


def test_sold_stats_median_min_max(tmp_path):
    sold_path = tmp_path / "gpu_sold.csv"
    archive_store.record_sold(
        [_sold_row(f"https://x/{i}", price=p) for i, p in enumerate([50.0, 100.0, 300.0])],
        str(sold_path),
    )
    got = archive_store.sold_stats(str(sold_path))
    assert got["count"] == 3
    assert got["median"] == 100.0
    assert got["min"] == 50.0
    assert got["max"] == 300.0


def test_sold_stats_skips_blank_and_garbage_prices(tmp_path):
    """Only rows with numeric prices count; blank/garbage rows are silently dropped."""
    sold_path = tmp_path / "gpu_sold.csv"
    with open(sold_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=archive_store.SOLD_FIELDS)
        w.writeheader()
        w.writerow({"timestamp": "t", "name": "x", "condition": "", "price": "100.0",
                    "url": "https://x/a", "detected_via": "badge_feed"})
        w.writerow({"timestamp": "t", "name": "x", "condition": "", "price": "",
                    "url": "https://x/b", "detected_via": "badge_feed"})
        w.writerow({"timestamp": "t", "name": "x", "condition": "", "price": "gibberish",
                    "url": "https://x/c", "detected_via": "badge_feed"})
    got = archive_store.sold_stats(str(sold_path))
    assert got["count"] == 1
    assert got["median"] == 100.0


# ── Capture point A — extract_listings-shaped payload → badge_feed ────────────


def test_capture_point_a_badge_feed_lands(tmp_path):
    """Sold cards coming off the feed get archived with detected_via=badge_feed."""
    log_file = str(tmp_path / "gpu_prices.csv")
    sold_from_feed = [{"name": "RTX 5070", "condition": "Μεταχειρισμένο",
                       "price": 105.0, "price_raw": "105€", "url": "https://x/a"}]
    tagged = [{**r, "detected_via": "badge_feed"} for r in sold_from_feed]
    archive_store.record_sold(tagged, archive_store.sold_path_for(log_file))
    rows = _read(archive_store.sold_path_for(log_file))
    assert len(rows) == 1
    assert rows[0]["detected_via"] == "badge_feed"


# ── Capture point B — verify_sold's filter step → badge_page ──────────────────


def test_capture_point_b_badge_page_preserves_original_timestamps(tmp_path):
    """verify_sold splits live rows into kept/removed; archived rows keep their CSV timestamps."""
    log_file = str(tmp_path / "gpu_prices.csv")
    rows = [
        {"timestamp": "2026-07-20 10:00:00", "name": "RTX 5070", "condition": "New",
         "price": "130.0", "url": "https://x/a"},
        {"timestamp": "2026-07-21 11:00:00", "name": "RX 5700", "condition": "Used",
         "price": "105.9", "url": "https://x/b"},
    ]
    sold = {"https://x/a"}
    removed_rows = [r for r in rows if (r.get("url") or "").strip() in sold]
    tagged = [{**r, "detected_via": "badge_page"} for r in removed_rows]
    archive_store.record_sold(tagged, archive_store.sold_path_for(log_file))
    got = _read(archive_store.sold_path_for(log_file))
    assert len(got) == 1
    assert got[0]["url"] == "https://x/a"
    assert got[0]["detected_via"] == "badge_page"
    assert got[0]["timestamp"] == "2026-07-20 10:00:00"


# ── Capture point C — ai_verify's read-then-archive step → ai_verify ──────────


def test_capture_point_c_ai_verify_reads_live_csv_and_archives(tmp_path):
    """run_ai_verify re-reads the live CSV, picks out sold URLs, archives them."""
    log_file = tmp_path / "gpu_prices.csv"
    with open(log_file, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["timestamp", "name", "condition", "price", "url"])
        w.writeheader()
        w.writerow({"timestamp": "2026-07-22 10:00:00", "name": "RTX 5070",
                    "condition": "Used", "price": "150.0", "url": "https://x/a"})
        w.writerow({"timestamp": "2026-07-22 10:00:00", "name": "RTX 5060",
                    "condition": "Used", "price": "120.0", "url": "https://x/b"})
    from verify import _archive_ai_verify_sold
    _archive_ai_verify_sold(str(log_file), {"https://x/a"})
    got = _read(archive_store.sold_path_for(str(log_file)))
    assert len(got) == 1
    assert got[0]["url"] == "https://x/a"
    assert got[0]["detected_via"] == "ai_verify"
    assert got[0]["price"] == "150.0"
