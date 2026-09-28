"""Every golden test file must be listed in the safety-net runner.

``pyproject.toml`` limits pytest to ``tests/unit``, so ``tests/golden/`` runs
only through ``tests/golden/run_safety_net.py`` and its ``MODULES`` list. A
golden file missing from that list never runs anywhere. The runner is parsed,
not imported: importing it would set ``FYP_BAKED_CONTRACTS_ONLY`` for the rest
of the test session.
"""

import re
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"


def test_every_golden_test_is_in_the_runner():
    runner = (GOLDEN_DIR / "run_safety_net.py").read_text(encoding="utf-8")
    listed = set(re.findall(r'GOLDEN_DIR / "(test_\w+\.py)"', runner))
    on_disk = {p.name for p in GOLDEN_DIR.glob("test_*.py")}
    missing = sorted(on_disk - listed)
    assert not missing, f"golden tests not listed in run_safety_net.MODULES: {missing}"
    stale = sorted(listed - on_disk)
    assert not stale, f"run_safety_net.MODULES lists missing files: {stale}"
