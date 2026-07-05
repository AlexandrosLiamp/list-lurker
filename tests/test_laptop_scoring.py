"""laptop_perf parses CPU/GPU/RAM/SSD specs out of free-text retail titles and
computes a combined value score (cpu*0.5 + gpu*0.5)."""

from laptop_perf import get_laptop_scores


def test_full_spec_title():
    s = get_laptop_scores("Lenovo Legion 5 15.6 (Ryzen 7 5800H/16GB/512GB SSD/RTX 3060)")
    assert s["cpu_name"] == "AMD Ryzen 7 5800H"
    assert s["gpu_name"] == "GeForce RTX 3060 Laptop"
    assert s["ram_gb"] == 16
    assert s["ssd_gb"] == 512
    assert s["combined_score"] == round(s["cpu_score"] * 0.5 + s["gpu_score"] * 0.5, 1)
    assert s["combined_score"] > 50


def test_unknown_cpu_falls_back_to_integrated():
    s = get_laptop_scores("HP 250 G8 15.6 (i3-1115G4/8GB/256GB SSD)")
    assert s["gpu_name"] == "Integrated Graphics"
    assert s["gpu_score"] > 0
    assert s["ram_gb"] == 8
    assert s["ssd_gb"] == 256


def test_no_specs_at_all():
    s = get_laptop_scores("laptop για ανταλλακτικά")
    assert s["gpu_name"] == "Integrated Graphics"
    assert s["combined_score"] >= 0
