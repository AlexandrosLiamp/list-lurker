"""Price-aware URL dedup — the fix for the stale-price rescan bug.

Once a listing's URL is known, a scanner used to skip it forever regardless of
price. That silently dropped every subsequent price change. new_unique now
re-emits a known URL when its price actually changed; load_known_prices reads
back the LATEST recorded price per URL so the comparison uses fresh state.
"""

import csv

import crawl_utils
import archive_store


FIELDS = ["timestamp", "name", "condition", "price", "url"]


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _row(url, price, ts="2026-07-22 10:00:00", name="RTX 5070"):
    return {"timestamp": ts, "name": name, "condition": "Μεταχειρισμένο",
            "price": "" if price is None else price, "url": url}


# ── new_unique ────────────────────────────────────────────────────────────────


def test_new_unique_returns_price_changed_known_url():
    """The bug the whole plan exists for: a known URL whose price dropped is 'new'."""
    known = {"https://example.com/a": 130.0}
    items = [{"url": "https://example.com/a", "price": 105.0, "name": "x"}]
    assert crawl_utils.new_unique(items, known) == items


def test_new_unique_excludes_unchanged_known_url():
    """Regression guard: unchanged URLs must never re-log (no Discord spam)."""
    known = {"https://example.com/a": 130.0}
    items = [{"url": "https://example.com/a", "price": 130.0, "name": "x"}]
    assert crawl_utils.new_unique(items, known) == []


def test_new_unique_still_dedups_within_batch():
    """Regression: overlapping pages surfacing the same URL twice still collapse."""
    known: dict[str, float | None] = {}
    dup = {"url": "https://example.com/a", "price": 100.0, "name": "x"}
    assert crawl_utils.new_unique([dup, dup], known) == [dup]


def test_new_unique_includes_new_url_when_known_is_dict():
    """Unknown URLs are always emitted."""
    known: dict[str, float | None] = {"https://example.com/a": 100.0}
    items = [{"url": "https://example.com/b", "price": 200.0, "name": "y"}]
    assert crawl_utils.new_unique(items, known) == items


# ── price_changed ─────────────────────────────────────────────────────────────


def test_price_changed_different_values():
    assert crawl_utils.price_changed(130.0, 105.0) is True


def test_price_changed_ignores_float_rounding_noise():
    assert crawl_utils.price_changed(130.0, 129.999999999) is False


def test_price_changed_equal_values():
    assert crawl_utils.price_changed(130.0, 130.0) is False


def test_price_changed_none_side_is_never_a_change():
    """Blank/unparseable prices must not register as changes — a scrape flake
    with a missing price would otherwise spam re-logs and re-alerts."""
    assert crawl_utils.price_changed(None, 100.0) is False
    assert crawl_utils.price_changed(100.0, None) is False
    assert crawl_utils.price_changed(None, None) is False


# ── _known_streak_checker ─────────────────────────────────────────────────────


def test_streak_checker_still_early_stops_on_dict_known():
    known = {f"https://example.com/{i}": 100.0 for i in range(5)}
    check = crawl_utils._known_streak_checker(known, threshold=3)
    listings = [{"url": f"https://example.com/{i}", "price": 100.0} for i in range(5)]
    assert check(listings) is True


def test_streak_checker_no_threshold_never_stops():
    known = {"https://example.com/a": 100.0}
    check = crawl_utils._known_streak_checker(known, threshold=None)
    assert check([{"url": "https://example.com/a", "price": 100.0}]) is False


# ── load_known_prices ─────────────────────────────────────────────────────────


def test_load_known_prices_returns_latest_row_per_url(tmp_path):
    csv_path = tmp_path / "gpu.csv"
    _write_csv(csv_path, [
        _row("https://example.com/a", 130.0, ts="2026-07-20 10:00:00"),
        _row("https://example.com/a", 105.0, ts="2026-07-22 10:00:00"),  # later wins
        _row("https://example.com/b", 200.0),
    ])
    known = crawl_utils.load_known_prices(str(csv_path))
    assert known == {"https://example.com/a": 105.0, "https://example.com/b": 200.0}


def test_load_known_prices_no_10x_inflation_on_canonical_float(tmp_path):
    """The classic parse_price('140.0') → 1400.0 trap must not fire at this layer.
    Regression guard mirroring test_parse_price.test_csv_price_regression_10x_bug."""
    csv_path = tmp_path / "gpu.csv"
    _write_csv(csv_path, [_row("https://example.com/a", "140.0")])
    known = crawl_utils.load_known_prices(str(csv_path))
    assert known == {"https://example.com/a": 140.0}


def test_load_known_prices_blank_price_stored_as_none(tmp_path):
    csv_path = tmp_path / "gpu.csv"
    _write_csv(csv_path, [_row("https://example.com/a", None)])
    known = crawl_utils.load_known_prices(str(csv_path))
    assert known == {"https://example.com/a": None}


def test_load_known_prices_missing_file_returns_empty_dict(tmp_path):
    assert crawl_utils.load_known_prices(str(tmp_path / "nope.csv")) == {}


# ── archive_store.fold_into_archive last-wins ─────────────────────────────────


def test_fold_into_archive_keeps_latest_price_per_url(tmp_path, monkeypatch):
    live = tmp_path / "live.csv"
    arch = tmp_path / "arch.csv"
    _write_csv(live, [
        _row("https://example.com/a", 130.0, ts="2026-07-20 10:00:00"),
        _row("https://example.com/a", 105.0, ts="2026-07-22 10:00:00"),  # last wins
    ])
    added = archive_store.fold_into_archive(str(live), str(arch))
    assert added == 1
    with open(arch, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["url"] == "https://example.com/a"
    assert rows[0]["price"] == "105.0"
