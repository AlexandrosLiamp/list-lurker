"""GPU model matching against the 91-model score database. Keys are checked
longest-first so that e.g. an 'RTX 3080 Ti' never matches the plain 'RTX 3080'."""

import gpu_perf
import monitor


def test_matches_model_case_insensitively():
    display, score = monitor.match_gpu("NVIDIA GeForce RTX 3080 10GB Founders Edition")
    assert display == "GeForce RTX 3080"
    assert score > 0


def test_longer_key_wins_over_substring():
    display, _ = monitor.match_gpu("rtx 3080 ti gaming x trio")
    assert display == "GeForce RTX 3080 Ti"


def test_no_gpu_returns_none():
    assert monitor.match_gpu("Intel NUC mini pc i5") is None


def test_gpu_perf_db_size_and_consistency():
    assert len(gpu_perf.GPU_MODELS) >= 90
    # both copies of the matcher must agree on a common model
    assert monitor.match_gpu("radeon rx 6700 xt") is not None
    assert gpu_perf.score_for("RX 6700 XT 12GB") == monitor.match_gpu("rx 6700 xt")[1]
