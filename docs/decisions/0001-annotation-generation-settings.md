# 0001. Annotation generation settings: HIGH media resolution, temperature 0

Date: 2026-06-19

## Context

The Gemini generation settings for machine annotation were chosen from A/B
runs in spring 2026, when Gemini was the only annotation backend. All runs
annotated the same local-video sample with structured output
(`gemini-3-flash-preview`), the same prompt and schema, and passed both arms
through the identical recode downstream, so only the parameter under test
varied. Comparison was field-type-aware: enum → exact-match agreement, list →
mean Jaccard, numeric → correlation, free text → coverage. The same
comparison logic now lives in `fyp/annotation/ab_eval.py`, which the A/B
evaluation panel on Admin → Contracts uses.

**The noise floor.** At a non-zero temperature the model is stochastic, so
two identical runs disagree. Every test therefore ran a same-setting control
and treated its agreement as the floor: a gap counts as a real effect only
where it falls below that floor.

**Sample.** 80 videos from a maintainer-local convenience corpus
(`--seed 17`), not text- or brand-heavy. Some sensitive fields have low
coverage (e.g. `symbols_and_brands` ~0.55), so those rates rest on ~40
videos. The one-off spike scripts and raw outputs are not shipped, so these
exact runs cannot be reproduced from the repository; equivalent runs on
other data use the A/B evaluation panel with the same metrics:

- media resolution: arms HIGH vs LOW, n=80, seed 17;
- temperature: arms temp 0.0 vs 1.0, n=80, seed 17.

### Media resolution

Settable values are LOW / MEDIUM / HIGH (+ UNSPECIFIED). For video, LOW and
MEDIUM are equivalent (~70 tokens per frame; MEDIUM and LOW produced
byte-identical prompt-token counts, e.g. 5359 == 5359, 9147 == 9147); HIGH
is ~280 per frame. For video the setting is effectively binary, LOW (=
MEDIUM) vs HIGH, with no useful middle ground.

- **Cost:** LOW cut mean prompt tokens from 8,814 to 5,207 (−41%).
- **Overall quality:** enum agreement was identical (0.887 HIGH-vs-LOW ==
  0.887 floor); categorically, LOW behaves like a second HIGH run.
- **Within noise (LOW costs nothing real):** `text_overlays` (the OCR worry,
  gap +0.03), `main_ethnicity`, `main_gender`, faces, `content_category`,
  `type_of_story` (mostly), `objects` (its low Jaccard is naming-vocabulary
  noise, present between two HIGH runs too), yes/no flags, audio.
- **Degraded by LOW (gap below the floor):** `symbols_and_brands` (brand and
  logo detection, +0.09), `sensitivity_score` (+0.12), and softer derived
  fields (`scene_energy` +0.20, `main_activity` +0.14, sparse
  `call_to_action`).
- Reliability was about equal (LOW had 2/80 transient DNFs; retry absorbs
  them).

### Temperature and repetition penalties

The migration to Gemini 3 had raised temperature from 0.0 to 1.0 on Google's
general guidance (below 1.0 risks looping or degraded reasoning on thinking
models). That guidance targets unconstrained free text; annotation is
schema-constrained, so it was tested. Four arms, n=80, all HIGH:
`t1.0_noPen` (production at the time), `t0_noPen`, `t0_pen`, and `t0_pen_b`
(a temp-0 reproducibility pair).

- **No looping at temp 0, with or without penalties.** No arm had a
  MAX_TOKENS finish; the only non-STOP finishes were transient
  `DNF - see error`s. The free-text repeat ratio was essentially identical
  across all four arms (mean ~0.011–0.015, max ~0.063–0.067), as were
  thinking-token counts. The looping failure mode does not occur under
  constrained decoding.
- **Penalties are a no-op.** Penalties-off vs penalties-on agreed at 0.926
  (enum), the same as the penalties-on vs penalties-on reproducibility floor
  (0.928): toggling them changes the output no more than re-running the
  model does. Their original job (suppressing free-text looping) is gone; in
  multi-field annotation they can only discourage legitimate cross-field
  repetition (an entity in `faces_ethnicity` and `main_ethnicity`; a brand
  in `symbols_and_brands` and the scene text; repeated correct enum values),
  and they also penalize the thinking trace. (The structured path already
  ignored them; they remained only on the unused free-text path.)
- **Temp 0 is markedly more reproducible:**

  | metric | temp=1.0 floor | temp=0 floor (penalties on) |
  |---|---|---|
  | enum agreement | 0.887 | 0.928 |
  | list Jaccard | 0.520 | 0.738 |
  | numeric correlation | 0.767 | 0.870 |

  Lists (objects, categories, symbols) and numeric scores stabilize the
  most.
- **Temp 0 does not change the content.** Temp 0 vs temp 1.0 agreed at 0.889
  (enum), essentially the temp-1.0 run-to-run floor (0.887): temp 0 draws
  from the same distribution, more tightly.
- **Caveat:** temp 0 is not fully deterministic (enum ~0.928, not 1.0);
  distributed inference adds irreducible non-determinism. It reduces
  volatility; it cannot eliminate it.

## Decision

- Keep media resolution **HIGH** (`media_resolution = ""`, the API default,
  which is HIGH for video). The ~41% saving is real and most fields survive,
  but LOW measurably hurts brand/logo recognition and sensitivity scoring. If
  those two fields are ever deemed non-critical, `"LOW"` is a clean ~41%
  input-cost win. A middle ground, if needed, is selective HIGH (LOW
  globally plus HIGH re-annotation of brand-relevant slices), not MEDIUM.
- Set **`temperature = 0.0`** with repetition penalties **off** — a
  deliberate, measured step away from Google's general default: for this
  constrained structured task temp 0 does not loop, does not change the
  content, removes a dead knob, and gives the reproducibility a research
  instrument needs.

Resulting settings (now in the `[machine.gemini]` block of
`config/config.toml`):

| setting | value | why |
|---|---|---|
| `model` | `gemini-3-flash-preview` | — |
| `media_resolution` | `""` (= HIGH for video) | HIGH preserves brand/logo and sensitivity fields |
| `temperature` | `0.0` | reproducible; no looping; content unchanged |

## Consequences

- Annotation backends became pluggable after these runs; the settings are
  the `[machine.gemini]` block (see [configuration.md](../configuration.md)).
- Keys named in the original results table no longer exist: structured
  output is always on (there is no `use_structured_output` toggle — the
  response schema is generated from the annotation contract), and
  `presence_penalty` / `frequency_penalty` were removed with the legacy
  free-text path they served.
- Each generation-config change mints a new annotation version
  (`fyp/annotation/annotation_versioning.py`), so earlier annotations stay
  intact and queryable; the move to temperature 0.0 was one such version.
- The experiments can be repeated on any installation's own data with the
  A/B evaluation panel (Admin → Contracts), which applies the same metrics.
