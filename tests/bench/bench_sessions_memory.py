"""E1/E2 memory acceptance benchmarks for the batched sessions build.

Not a unit test — a script to run before deploying, on a machine that has the
real local store. Each condition runs in a CLEAN subprocess (ru_maxrss is a
process high-water mark) against a SCRATCH copy of the store, so the real
data root is never touched.

E1 — corpus-independence (the decisive experiment): duplicate every embedding
shard with suffixed item_ids -> a real 2x store at zero embedding cost. Run
the same batch size against 1x and 2x; peak RSS must differ by < 10%. If it
roughly doubles, an O(corpus) term is still hiding.

E2 — batch-linearity: sweep batch sizes on the 1x store and fit
peak = a + b * batch. `a` is the corpus-independent intercept (id index +
interpreter + Arrow); `b` sets MAX_VECTORS_PER_LINK empirically.

Usage:
    python tests/bench/bench_sessions_memory.py --data-root ~/fyp_local \\
        [--e1] [--e2] [--batches 1,2,4,8,16] [--collections 24]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_CHILD = r"""
import json, resource, sys
import numpy as np
sys.path.insert(0, {root!r})
import os
os.environ["FYP_CONFIG_PATH"] = {config!r}
from fyp.analysis import embedding_store, embeddings, session_explorer as se

model = embeddings.active_embedding_backend().model_id()
mean, count, fp = embedding_store.get_corpus_mean(model)
index = embedding_store.load_index(model)
cids = [c for c, _ in se.discover_collections()][: {n_collections}]
peak0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
for start in range(0, len(cids), {batch}):
    se.build_batch(cids[start:start + {batch}], model, mean, index)
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
print(json.dumps({{"n_vectors": count, "batch": {batch},
                   "peak_mb": peak, "baseline_mb": peak0}}))
"""






def _write_scratch_config(scratch: Path, data_root: Path) -> Path:
    """Minimal config TOML pointing every location at the scratch root."""
    src = PROJECT_ROOT / "config" / "config.toml"
    text = src.read_text()
    cfg = scratch / "config.toml"
    cfg.write_text(text.replace('local_data = "~/fyp_local"',
                                f'local_data = "{scratch / "data"}"'))
    return cfg






def _prepare_store(data_root: Path, scratch: Path, duplicate: bool) -> None:
    """Copy the needed inputs into scratch; optionally 2x the shard store."""
    import pandas as pd
    import pyarrow as pa

    dst = scratch / "data" / "recoded"
    dst.mkdir(parents=True)
    (scratch / "data" / "cache").mkdir(parents=True)
    src = data_root / "recoded"
    needed = ["collections_recoded.parquet", "video_map.parquet",
              "scrapes_recoded.parquet", "machine_annotations_recoded.parquet"]
    for fn in needed:
        if (src / fn).exists():
            shutil.copy2(src / fn, dst / fn)
    for shard in src.glob("video_embeddings__*.parquet"):
        shutil.copy2(shard, dst / shard.name)
        if duplicate:
            df = pd.read_parquet(shard)
            df["item_id"] = df["item_id"].astype(str) + "_dup"
            df.to_parquet(dst / f"video_embeddings__dup_{shard.name.split('__')[1]}",
                          engine="pyarrow")






def _run_condition(config: Path, batch: int, n_collections: int) -> dict:
    code = _CHILD.format(root=str(PROJECT_ROOT), config=str(config),
                         batch=batch, n_collections=n_collections)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])






def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="~/fyp_local")
    ap.add_argument("--e1", action="store_true", help="corpus-independence (needs ~1.5x store size of scratch disk)")
    ap.add_argument("--e2", action="store_true", help="batch-linearity sweep")
    ap.add_argument("--batches", default="1,2,4,8,16")
    ap.add_argument("--collections", type=int, default=24,
                    help="How many (largest) collections each condition segments")
    args = ap.parse_args()
    data_root = Path(os.path.expanduser(args.data_root))
    if not (args.e1 or args.e2):
        args.e1 = args.e2 = True

    if args.e2:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            _prepare_store(data_root, scratch, duplicate=False)
            config = _write_scratch_config(scratch, data_root)
            print("== E2: batch-linearity (1x store) ==")
            rows = []
            for b in [int(x) for x in args.batches.split(",")]:
                r = _run_condition(config, b, args.collections)
                rows.append(r)
                print(f"  batch={b:3d}  peak={r['peak_mb']:8.0f} MB  baseline={r['baseline_mb']:8.0f} MB")
            if len(rows) >= 2:
                import numpy as np

                bs = np.array([r["batch"] for r in rows], dtype=float)
                pk = np.array([r["peak_mb"] for r in rows], dtype=float)
                slope, intercept = np.polyfit(bs, pk, 1)
                print(f"  fit: peak ≈ {intercept:.0f} MB + {slope:.1f} MB/collection")

    if args.e1:
        results = {}
        for label, dup in (("1x", False), ("2x", True)):
            with tempfile.TemporaryDirectory() as tmp:
                scratch = Path(tmp)
                _prepare_store(data_root, scratch, duplicate=dup)
                config = _write_scratch_config(scratch, data_root)
                results[label] = _run_condition(config, 8, args.collections)
                print(f"== E1 {label}: vectors={results[label]['n_vectors']:,} "
                      f"peak={results[label]['peak_mb']:.0f} MB ==")
        ratio = results["2x"]["peak_mb"] / results["1x"]["peak_mb"]
        verdict = "PASS" if ratio < 1.10 else "FAIL — O(corpus) term still present"
        print(f"E1 peak ratio 2x/1x = {ratio:.3f}  ->  {verdict}")


if __name__ == "__main__":
    main()
