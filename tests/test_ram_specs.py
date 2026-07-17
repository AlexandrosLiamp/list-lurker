"""RAM title parsing + sanity flags (ram_specs.py), incl. the 2x8GB kit regression."""
import ram_specs
from ram_specs import check_ram, is_desktop_ddr45, max_capacity_gb, parse_ram_title


def test_kit_notation_counts_whole_kit():
    # regression: the old parser read "2x8GB" as 8 GB and rejected valid 16 GB kits
    assert max_capacity_gb("Corsair Vengeance 2x8GB DDR4 3200") == 16
    assert max_capacity_gb("G.Skill 32GB (2x16GB) DDR5-6000") == 32
    assert max_capacity_gb("Kingston 2 x 4 GB DDR4") == 8


def test_bare_g_and_gib_tolerated():
    assert max_capacity_gb("ddr4 16g 3200mhz") == 16
    assert max_capacity_gb("8GiB DDR4 SODIMM") == 8
    # timings/speeds never misread as capacity
    assert max_capacity_gb("DDR4 3200 CL16") is None


def test_parse_gen_and_speed():
    p = parse_ram_title("TeamGroup DDR5-6000 2x16GB")
    assert (p["gen"], p["mhz"], p["kit"], p["total_gb"]) == (5, 6000, (2, 16), 32)


def test_sodimm_and_old_gen_flags():
    assert "sodimm" in check_ram("Crucial 16GB DDR4 3200 SODIMM laptop")
    assert "old_gen" in check_ram("Kingston 8GB DDR3 1600")
    assert not is_desktop_ddr45("Crucial 16GB DDR4 3200 SODIMM")
    assert not is_desktop_ddr45("Kingston 8GB DDR3 1600")


def test_speed_gen_mismatch():
    assert "speed_gen_mismatch" in check_ram("16GB DDR4 6000MHz")
    assert "speed_gen_mismatch" in check_ram("32GB DDR5 2400MHz")
    assert "speed_gen_mismatch" not in check_ram("16GB DDR4 3200MHz")


def test_capacity_kit_mismatch():
    assert "capacity_kit_mismatch" in check_ram("32GB kit 2x8GB DDR4 3200")
    # total == kit product, or token is the per-module size: both fine
    assert "capacity_kit_mismatch" not in check_ram("16GB (2x8GB) DDR4 3200")


def test_per_stick_price_suspect():
    # 2x16 kit "for 20€" when the market median is 3 €/GB → per-stick or bait
    assert "per_stick_price_suspect" in check_ram(
        "Corsair 2x16GB DDR5 6000", price=20, median_eur_per_gb=3.0)
    assert "per_stick_price_suspect" not in check_ram(
        "Corsair 2x16GB DDR5 6000", price=90, median_eur_per_gb=3.0)


def test_clean_listing_has_no_flags():
    assert check_ram("Kingston Fury Beast 16GB 2x8GB DDR4 3200MHz", price=45,
                     median_eur_per_gb=3.0) == []
    assert is_desktop_ddr45("Kingston Fury Beast 16GB 2x8GB DDR4 3200MHz")
