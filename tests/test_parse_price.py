"""parse_price handles RAW scraped European-formatted strings; csv_price handles
values re-read from our own CSVs (canonical floats). Mixing them up caused the
historical 10x price-inflation bug: parse_price("140.0") treats '.' as a thousands
separator and returns 1400.0. csv_price exists so CSV re-reads never do that."""

import monitor


def test_european_thousands_and_decimal():
    assert monitor.parse_price("1.234,56 €") == 1234.56


def test_plain_euro():
    assert monitor.parse_price("85€") == 85.0


def test_thousands_only():
    assert monitor.parse_price("1.100 EUR") == 1100.0


def test_no_digits_is_none():
    assert monitor.parse_price("τηλεφωνήστε μου") is None


def test_inflation_trap_documented():
    # This IS the wrong tool for CSV floats — kept as documentation of the trap.
    assert monitor.parse_price("140.0") == 1400.0


def test_csv_price_regression_10x_bug():
    # Regression guard: CSV floats must round-trip unscaled.
    assert monitor.csv_price("140.0") == 140.0
    assert monitor.csv_price(140.0) == 140.0


def test_csv_price_legacy_raw_rows_still_parse():
    assert monitor.csv_price("1.234,56 €") == 1234.56


def test_csv_price_empty_is_none():
    assert monitor.csv_price("") is None
    assert monitor.csv_price(None) is None
