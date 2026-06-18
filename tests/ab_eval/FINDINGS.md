# Annotation generation-config A/B findings

Empirical basis for the `[machine]` settings in `config/config.toml`. All tests
annotate the same local-video sample, structured output (Gemini-3-flash-preview),
the same prompt/schema, and run both arms through the **identical** recode
downstream — so the only thing varying is the parameter under test. Comparison is
field-type-aware (`tests/ab_eval/_ab_common.py`): enum → exact-match agreement,
list → mean Jaccard, numeric → correlation, free-text → coverage.

**Method note — the noise floor.** At a non-zero temperature the model is
stochastic, so two *identical* runs disagree. To tell "real effect" from
"sampling noise" we always run a same-setting control and treat its agreement as
the floor: a gap only counts as a real effect where it falls **below** that floor.

Sample: 80 local videos, `--seed 17`, from `/Users/<user>/Desktop/fyp_mini/media`.
Caveat: a convenience sample, not text/brand-heavy — some sensitive fields have
low coverage (e.g. `symbols_and_brands` ~0.55), so those rates rest on ~40 videos.

Reproduce:
```
python tests/ab_eval/media_resolution_ab.py --n 80 --seed 17 --arm-a HIGH --arm-b LOW   # quality
python tests/ab_eval/media_resolution_ab.py --n 80 --seed 17 --arm-a HIGH --arm-b HIGH  # noise floor
python tests/ab_eval/temperature_ab.py       --n 80 --seed 17                            # temp/penalty
```
Raw outputs: `tests/ab_eval/results/media_res_ab_*.json`, `temperature_ab_n80.json`.

---

## 1. media_resolution → **KEEP HIGH** (`media_resolution = ""`, = API default = HIGH for video)

**Settable values:** LOW / MEDIUM / HIGH (+ UNSPECIFIED). For **video**, LOW and
MEDIUM are equivalent (~70 tokens/frame; verified — MEDIUM and LOW produced
byte-identical prompt-token counts, e.g. 5359 == 5359, 9147 == 9147); HIGH is
~280/frame. So for video `media_resolution` is effectively **binary**: LOW(=MEDIUM)
vs HIGH. There is no useful middle ground.

**Cost lever (real):** LOW cut mean prompt tokens **8,814 → 5,207 (−41%)**.

**Quality, against the HIGH-vs-HIGH noise floor (n=80):**
- Overall enum agreement was **identical** (0.887 HIGH-vs-LOW == 0.887 floor) —
  categorically LOW ≈ a second HIGH run.
- **Within noise (LOW costs nothing real):** `text_overlays` (the OCR worry — gap
  +0.03), `main_ethnicity`, `main_gender`, faces, `content_category`,
  `type_of_story` (mostly), `objects` (its low Jaccard is naming-vocabulary noise,
  present between two HIGH runs too), yes/no flags, audio.
- **Genuinely degraded by LOW (gap below the floor):** `symbols_and_brands`
  (brand/logo detection, +0.09), `sensitivity_score` (+0.12), and softer
  derived fields (`scene_energy` +0.20, `main_activity` +0.14, sparse
  `call_to_action`).
- Reliability ~equal (LOW 2/80 transient DNFs; retry absorbs them).

**Decision:** keep **HIGH**. The ~41% saving is real and most fields survive, but
LOW measurably hurts brand/logo recognition and sensitivity scoring, which we are
not willing to trade. If those two fields are ever deemed non-critical, `"LOW"` is
a clean ~41% input-cost win. A "middle ground" if needed is *selective* HIGH
(LOW globally + HIGH re-annotation of brand-relevant slices), not MEDIUM.

---

## 2. temperature → **0.0**, repetition penalties **OFF**

Migration to Gemini 3 had raised temperature 0.0 → 1.0 on Google's general
guidance (sub-1.0 risks looping/degraded reasoning on thinking models). That
guidance targets **unconstrained free-text**; this task is schema-constrained, so
we tested it. Four arms (n=80, all HIGH): `t1.0_noPen` (prod), `t0_noPen`,
`t0_pen`, and `t0_pen_b` (a temp-0 reproducibility pair).

**(a) No looping at temp=0 — with or without penalties.** Zero MAX_TOKENS
finishes in any arm; the only non-STOPs were transient `DNF - see error`s. The
free-text repeat-ratio was essentially identical across all four arms (mean
~0.011–0.015, max ~0.063–0.067), as were thinking-token counts. Google's looping
failure mode does not occur under constrained decoding.

**(b) Penalties are a no-op (so: off).** `penalties-OFF vs penalties-ON` agreed at
**0.926** (enum) — the same as the `penalties-ON vs penalties-ON` reproducibility
floor (**0.928**). I.e. toggling the penalties changes the output no more than
re-running the model does. Their original job (suppress free-text looping) is gone;
for multi-field annotation they can only *discourage legitimate cross-field
repetition* (an entity in `faces_ethnicity` **and** `main_ethnicity`; a brand in
`symbols_and_brands` **and** the scene text; repeated correct enum values) and they
also penalize the thinking trace. Downside with no upside → left **off** (the
structured path already ignored them; they remain only on the unused free-text path).

**(c) temp=0 is markedly more reproducible** (the reason for the change):
| metric | temp=1.0 floor | **temp=0 floor (penON)** |
|---|---|---|
| enum agreement | 0.887 | **0.928** |
| list Jaccard | 0.520 | **0.738** |
| numeric correlation | 0.767 | **0.870** |

Lists (objects/categories/symbols) and numeric scores stabilize the most.

**(d) temp=0 does not change the content.** `temp=0 vs temp=1.0` agreement was
**0.889** (enum) — essentially equal to the temp=1.0 run-to-run floor (0.887). So
temp=0 draws from the same distribution as temp=1.0, just more tightly; no quality
or content regression from lowering it.

**Caveat:** temp=0 is **not** perfectly deterministic (enum ~0.928, not 1.0) —
distributed inference adds irreducible non-determinism. It reduces volatility a
lot; it cannot eliminate it.

**Decision:** `temperature = 0.0`, penalties off. Justified as a deliberate,
measured step off Google's *general* default: for this constrained structured task
temp=0 does not loop, does not change the content, removes a dead knob, and gives
the reproducibility/auditability a research instrument needs.

---

## Resulting config (`[machine]`)

| setting | value | why |
|---|---|---|
| `model` | gemini-3-flash-preview | — |
| `use_structured_output` | true | valid JSON, schema-enforced |
| `media_resolution` | `""` (= HIGH for video) | §1 — HIGH preserves brand/logo + sensitivity |
| `temperature` | `0.0` | §2 — reproducible; no looping; content unchanged |
| `presence_penalty` / `frequency_penalty` | 0.6 / 1.2 (free-text path only) | §2 — no-op under structured output; left for legacy path |

Each generation-config change mints a new `annotation_version` (see
`fyp/annotation_versioning.py`), so prior annotations remain intact and queryable.
The temperature change to 0.0 is one such version bump.
