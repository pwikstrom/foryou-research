# Annotation pipeline safety net

A cost-free regression + consistency suite that pins the behaviour of the
machine-annotation refinement pipeline. It was written to hold that behaviour
steady across the move to structured output, schema-driven prompt generation
and pluggable annotation backends, and it still guards the pipeline today.

It runs entirely on **saved raw Gemini responses** — no API calls, no cost — so
it can re-validate the full pipeline over real data on every change.

## What it guards

| Test | Guards |
|---|---|
| `test_refinement_golden.py` | `refine_one_raw_annotation_batch` (JSON repair → flatten → rare-column consolidation → transcript de-dup → schema recode → label clean-up) reproduces a frozen golden output exactly. Also asserts the pipeline is deterministic. |
| `test_schema_pipeline_consistency.py` | The four hand-synced sources of truth — the Gemini **prompt**, `flatten_one_machine_response`, `REQUIRED_KEYS`, and `var_schema` — stay in lockstep. Hard-fails on broken invariants (unregistered `recode_func`, invalid schema, duplicate names); alerts on any change to the cross-source coupling. |
| `tests/unit/test_recode_series_branches.py` | (pre-existing) Series/scalar parity of every `recode_func`. |
| `tests/unit/test_annotation_repair.py` | Characterization of the fragile JSON-repair / flatten helpers (`fuzzy_load_of_json_from_string`, `flatten_one_machine_response`, `_compress_embedded_repeats`, `_decode_valid_unicode_escapes`, `_remove_repetitions`) — the contract a structured-output flattener must honour or consciously change. |
| `tests/unit/test_schema_cell_parsers.py` | The strict (never-`eval`) grammar of the `parse_*` var_schema cell parsers. |

## Run it

```bash
source .venv/bin/activate           # or use .venv/bin/python directly
python tests/golden/run_safety_net.py    # runs the whole suite, exit 0 = green
```

Individual modules are standalone too: `python tests/golden/test_refinement_golden.py`.

## Fixtures (committed, frozen)

- `fixtures/raw_sample.json` — 150 real raw responses sampled across 286 raw files (incl. a few empty/DNF to cover the bad path).
- `fixtures/golden_refined.parquet` — normalized (all-string, round-trip-safe) expected refined output.
- `fixtures/var_schema.snapshot.csv` — the schema the golden was built against (pins recoding).
- `fixtures/coupling_baseline.json` — snapshot of the prompt↔flatten↔required↔schema coupling.
- `fixtures/manifest.json` — provenance (counts, schema hash, models, prompt file).

## When a test fails

- **Golden diff** → the refinement output changed. The test prints exactly which
  columns/cells/rows moved. If the change is **intended**, re-bless:
  `python tests/golden/build_golden.py` and review the diff in the commit.
- **Coupling change** → a prompt field / schema row / required key moved relative
  to the others (e.g. a prompt field added without a schema row). If intended:
  `python tests/golden/test_schema_pipeline_consistency.py --bless`.

## Rebuilding the fixtures from scratch

```bash
python tests/golden/build_fixture.py        # resample raw responses
python tests/golden/build_golden.py          # re-freeze golden + schema snapshot
python tests/golden/test_schema_pipeline_consistency.py --bless
```

## Fragilities surfaced by this suite

- **(fixed)** `clean_up_machine_annotations` used to call `Series.sample(...)`
  with no `random_state`, making the whole refinement pipeline non-deterministic.
  It now passes `random_state=0`; `test_refinement_is_deterministic` guards it.
  Verified to be a no-op on existing output (a 500-sample length estimate is
  robust enough that no consolidation decision changed).
