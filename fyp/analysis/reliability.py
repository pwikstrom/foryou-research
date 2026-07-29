"""Per-variable annotation reliability estimates for disattenuation.

Harvests two existing subsystems, in priority order:

1. **Machine test-retest** — ab_eval runs whose arm pair used the *same*
   contract (equal ``etag``), the *same* backend and the *same* generation
   overrides. Two annotation passes of one instrument over one item set is a
   test-retest design, the textbook reliability estimate for the instrument
   actually producing the study data. Numeric fields use the test-retest
   correlation; enum fields the filled percent agreement (a crude,
   chance-uncorrected proxy — labelled as such); list fields the filled
   Jaccard.
2. **Human–machine ICR** — human-eval coding results (``results_coding.json``
   per run). Enum fields use Cohen's kappa (chance-corrected), numeric fields
   the correlation. Strictly this measures validity against a human coder and
   lower-bounds reliability; the source label says so.

The estimates are **item-level**. The Correlations tab's unit is the
collection-day group mean; the web service converts item-level reliability to
a conservative group-level value via Spearman–Brown at the configured minimum
group size before offering attenuation-corrected correlations.

``estimate_annotation_reliability()`` returns the artifact saved as
``annotation_reliability.json`` (location ``cache``) by the pca_refresh
worker.
"""

import json
import time

from fyp.logging_setup import get_logger

logger = get_logger(__name__)


RELIABILITY_FILENAME = "annotation_reliability.json"

# Estimates below this many compared items are too noisy to use.
MIN_ESTIMATE_N = 10






def _harvest_ab_eval_test_retest() -> dict:
    """Per-variable estimates from pure repeat-run ab_eval arm pairs.

    Returns {variable: {"reliability", "n", "kind", "source", "detail"}}.
    Newest runs win a collision within this source.
    """
    import fyp.annotation.ab_eval as ab

    out: dict = {}
    try:
        runs = ab.load_runs_index()
    except Exception as e:
        logger.warning(f"[reliability] cannot list ab_eval runs: {e}")
        return out

    # Oldest first, so newer runs overwrite older estimates.
    for entry in reversed(runs or []):
        if entry.get("status") != "complete":
            continue
        run_id = entry.get("run_id")
        try:
            run = ab.load_run(run_id)
        except Exception:
            continue
        manifest = (run or {}).get("manifest") or {}
        report = (run or {}).get("report") or {}
        arms = {a.get("name"): a for a in manifest.get("arms", [])}
        comparisons = report.get("comparisons") or {}

        for pair_key, comparison in comparisons.items():
            names = pair_key.split("|")
            if len(names) != 2:
                continue
            a, b = arms.get(names[0]), arms.get(names[1])
            if not a or not b:
                continue
            same_contract = a.get("etag") and a.get("etag") == b.get("etag")
            same_backend = a.get("backend") == b.get("backend")
            same_gen = json.dumps(a.get("gen_overrides") or {}, sort_keys=True) == \
                json.dumps(b.get("gen_overrides") or {}, sort_keys=True)
            if not (same_contract and same_backend and same_gen):
                continue

            detail = f"ab_eval run {run_id} ({names[0]} vs {names[1]}, backend {a.get('backend')})"
            for var, metrics in (comparison.get("columns") or {}).items():
                est = _estimate_from_ab_metrics(metrics)
                if est is None:
                    continue
                value, n, kind = est
                out[var] = {
                    "reliability": value,
                    "n": n,
                    "kind": kind,
                    "source": "machine test-retest",
                    "detail": detail,
                }
    return out






def _estimate_from_ab_metrics(metrics: dict):
    """Map one ab_eval column-metrics dict to (reliability, n, kind) or None."""
    kind = metrics.get("kind")
    if metrics.get("caveat"):
        return None
    if kind == "numeric":
        value = metrics.get("correlation")
        n = metrics.get("n_compared")
    elif kind == "enum":
        value = metrics.get("agreement_filled")
        n = metrics.get("n_filled_both")
    elif kind == "list":
        value = metrics.get("mean_jaccard_filled")
        n = metrics.get("n_filled_both")
    else:
        return None
    if value is None or n is None or n < MIN_ESTIMATE_N:
        return None
    value = float(value)
    if not (0.05 < value <= 1.0):
        return None
    return value, int(n), kind






def _harvest_human_eval_icr() -> dict:
    """Per-variable estimates from human-eval coding results (human vs machine).

    Enum fields use Cohen's kappa when present (chance-corrected); numeric
    fields the correlation. Returns the same shape as the ab_eval harvest.
    """
    from fyp.annotation import human_eval

    out: dict = {}
    try:
        tasks = human_eval.list_tasks()
    except Exception as e:
        logger.warning(f"[reliability] cannot list human-eval tasks: {e}")
        return out

    for task in tasks or []:
        run_id = task.get("run_id")
        task_type = task.get("task_type") or "coding"
        if task_type != "coding":
            continue
        try:
            results = human_eval.load_results(run_id, task_type)
        except Exception:
            continue
        if not results:
            continue

        for pair_key, comparison in (results.get("human_vs_machine") or {}).items():
            detail = f"human-eval run {run_id} ({pair_key})"
            for var, metrics in (comparison.get("columns") or {}).items():
                est = _estimate_from_human_metrics(metrics)
                if est is None:
                    continue
                value, n, kind = est
                existing = out.get(var)
                if existing is not None and existing["n"] >= n:
                    continue
                out[var] = {
                    "reliability": value,
                    "n": n,
                    "kind": kind,
                    "source": "human–machine agreement",
                    "detail": detail,
                }
    return out






def _estimate_from_human_metrics(metrics: dict):
    """Map one human-eval column-metrics dict to (reliability, n, kind) or None."""
    kind = metrics.get("kind")
    if metrics.get("caveat"):
        return None
    if kind == "enum":
        # Prefer the chance-corrected kappa injected by human_eval
        value = metrics.get("kappa")
        if value is None:
            value = metrics.get("agreement_filled")
        n = metrics.get("n_filled_both")
    elif kind == "numeric":
        value = metrics.get("correlation")
        n = metrics.get("n_compared")
    elif kind == "list":
        value = metrics.get("mean_jaccard_filled")
        n = metrics.get("n_filled_both")
    else:
        return None
    if value is None or n is None or n < MIN_ESTIMATE_N:
        return None
    value = float(value)
    if not (0.05 < value <= 1.0):
        return None
    return value, int(n), kind






def estimate_annotation_reliability() -> dict:
    """Combine both sources into the reliability artifact.

    Machine test-retest wins over human–machine agreement for the same
    variable (it estimates the reliability of the instrument that produced
    the study data; the human comparison is a validity-flavoured fallback).
    """
    human = _harvest_human_eval_icr()
    retest = _harvest_ab_eval_test_retest()

    variables = dict(human)
    variables.update(retest)

    logger.info(
        f"[reliability] {len(variables)} variables "
        f"({len(retest)} test-retest, {len(human)} human-machine)")

    return {
        "version": 1,
        "generated_at": time.time(),
        "min_estimate_n": MIN_ESTIMATE_N,
        "variables": variables,
    }
