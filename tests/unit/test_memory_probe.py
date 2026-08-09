"""Shared memory-probe helpers (fyp.core.memory) and the [MEM] log contract."""

import numpy as np
import pandas as pd

from fyp.memory import df_size_mb, mem_probe, peak_rss_mb, rss_mb


def test_rss_helpers_return_plausible_values():
    rss = rss_mb()
    peak = peak_rss_mb()
    assert rss > 10  # a Python process with pandas loaded is tens of MB
    assert peak >= rss * 0.5  # watermark can't be far below current RSS
    assert peak < 10_000_000  # sanity: not garbage units






def test_df_size_mb_matches_pandas():
    df = pd.DataFrame({"a": np.zeros(100_000), "b": ["x"] * 100_000})
    expected = df.memory_usage(deep=True).sum() / (1024**2)
    assert abs(df_size_mb(df) - expected) < 1e-9






def test_mem_probe_emits_convention_line():
    lines: list[str] = []
    with mem_probe("TESTTAG", "phase_one", log=lines.append, chunk=3, tier=1):
        pass
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("[TESTTAG][MEM] phase=phase_one ")
    for field in ("rss_start=", "rss_end=", "peak_during=", "peak_delta=+",
                  "chunk=3", "tier=1"):
        assert field in line






def test_mem_probe_logs_even_on_exception():
    lines: list[str] = []
    try:
        with mem_probe("TESTTAG", "boom", log=lines.append):
            raise ValueError("expected")
    except ValueError:
        pass
    assert len(lines) == 1 and "phase=boom" in lines[0]






def test_organize_datasets_aliases_point_at_shared_impl():
    from fyp import memory
    from fyp.analysis import organize_datasets as od

    assert od._rss_mb is memory.rss_mb
    assert od._peak_rss_mb is memory.peak_rss_mb
    assert od._df_size_mb is memory.df_size_mb






def test_flat_shim_is_same_module():
    import fyp.core.memory
    import fyp.memory

    assert fyp.memory is fyp.core.memory
