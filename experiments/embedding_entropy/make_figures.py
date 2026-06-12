"""Presentation figures for the embedding-entropy study (dark theme, 16:9).

Eight narrative-first PNGs for a policy/general audience, written to
``tmp/figs_presentation/``. Each figure leads with its takeaway as the title
and keeps statistical annotation light (a one-line method note in the footer).
1920x1080 px (12.8x7.2 in @ 150 dpi) so they drop straight into a dark deck.
"""

import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import FancyBboxPatch, Rectangle

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import data_access

OUT_DIR = os.environ.get("FYP_EXPERIMENT_TMP", os.path.join(_ROOT, "tmp"))
FIG_DIR = os.path.join(OUT_DIR, "figs_presentation")

# Dark-deck palette (colour-blind-aware accents on near-black).
BG = "#10131a"
PANEL = "#181c26"
FG = "#e8eaf0"
MUTED = "#8b93a7"
ACCENT = "#4fc3f7"      # cyan — primary data colour
ACCENT2 = "#ffb74d"     # amber — secondary / highlights
GOOD = "#81c784"        # green
BAD = "#e57373"         # red — used for the politics/contrast elements
VIOLET = "#b39ddb"

plt.rcParams.update({
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
    "text.color": FG, "axes.edgecolor": MUTED, "axes.labelcolor": FG,
    "xtick.color": MUTED, "ytick.color": MUTED, "font.family": "sans-serif",
    "axes.grid": False, "font.size": 15,
})
FIGSIZE = (12.8, 7.2)
DPI = 150




def _new_fig(title: str, subtitle: str, footer: str):
    """Create a styled figure with takeaway title, subtitle and method footer."""
    fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
    fig.subplots_adjust(top=0.80, bottom=0.14, left=0.09, right=0.95)
    fig.text(0.05, 0.93, title, fontsize=27, fontweight="bold", ha="left", color=FG)
    fig.text(0.05, 0.865, subtitle, fontsize=16, ha="left", color=MUTED)
    fig.text(0.05, 0.035, footer, fontsize=10.5, ha="left", color=MUTED, style="italic")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    return fig, ax




def _save(fig, name: str) -> None:
    path = os.path.join(FIG_DIR, name)
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")




def fig0_headline() -> None:
    """Stat-card title figure: the study in four numbers."""
    fig = plt.figure(figsize=FIGSIZE, dpi=DPI)
    fig.text(0.05, 0.88, "Do TikTok 'rabbit holes' exist?", fontsize=30,
             fontweight="bold", color=FG)
    fig.text(0.05, 0.80, "73 donated watch histories · 4.3M videos watched · "
             "260k videos mapped in semantic space", fontsize=15, color=MUTED)
    cards = [
        ("82%", "of people have at least\none genuine binge", ACCENT),
        ("0.2%", "of watch time is all\nbinges add up to", ACCENT2),
        ("6 min", "the typical binge:\n5 videos, then it's over", GOOD),
        ("0", "binges that 'led users\ndeeper' down a path", BAD),
    ]
    for i, (big, small, colour) in enumerate(cards):
        x = 0.05 + i * 0.235
        fig.patches.append(FancyBboxPatch(
            (x, 0.22), 0.215, 0.46, boxstyle="round,pad=0.012,rounding_size=0.02",
            transform=fig.transFigure, facecolor=PANEL, edgecolor=colour, lw=2))
        fig.text(x + 0.107, 0.54, big, fontsize=40, fontweight="bold",
                 color=colour, ha="center")
        fig.text(x + 0.107, 0.36, small, fontsize=14, color=FG, ha="center", va="center")
    fig.text(0.05, 0.13, "Binges are real, habitual — and brief, benign and commercial.",
             fontsize=17, color=FG)
    fig.text(0.05, 0.075, "The dramatic rabbit-hole narrative does not appear in these data.",
             fontsize=17, color=FG)
    _save(fig, "fig0_headline.png")




def fig1_rare_but_universal(sm: pd.DataFrame) -> None:
    """Per-donor share of watch time spent in binges."""
    v = pd.to_numeric(sm["frac_watchtime_in_episodes"], errors="coerce").fillna(0) * 100
    v = v.sort_values().reset_index(drop=True)
    fig, ax = _new_fig(
        "Nearly everyone binges — almost no one binges much",
        "Share of each person's total watch time spent in binge episodes (73 donors, sorted)",
        "Episode = ≥4 distinct, semantically similar videos in a row within one session "
        "(cosine distance < 0.5 on video embeddings). Median binger: 0.2% of watch time.")
    ax.bar(range(len(v)), v.clip(lower=0.004), color=ACCENT, width=0.8)
    ax.axhline(100, color=MUTED, lw=0.8, ls=":")
    ax.set_ylim(0, max(v.max() * 1.15, 3))
    ax.set_ylabel("% of watch time in binges")
    ax.set_xlabel("donors (each bar = one person)")
    ax.set_xticks([])
    med = float(v[v > 0].median())
    ax.annotate(f"median binger: {med:.1f}% of watch time",
                xy=(len(v) * 0.45, med + 0.15), fontsize=15, color=ACCENT2)
    ax.annotate("100% would mean all viewing was binges — every bar is near zero",
                xy=(2, max(v.max(), 3) * 0.92), fontsize=13, color=MUTED)
    _save(fig, "fig1_rare_but_universal.png")




def fig2_anatomy(ep: pd.DataFrame) -> None:
    """One real binge inside its ordinary session."""
    # A *typical*-sized binge (not the outlier monsters): 8-12 videos, <16 min.
    cand = ep[(ep["collection_id"].str.startswith("edcfa1f1"))
              & (ep["dominant_niche"] == "Beauty Product Reviews")
              & (ep["n_distinct"].between(8, 12)) & (ep["duration_min"] < 16)
              & (ep["dominant_niche_share"] >= 0.8)]
    e = cand.sort_values("focus").iloc[0]
    plays = data_access.load_plays([e["collection_id"]]).to_pandas()
    plays["_ts"] = pd.to_datetime(plays["local_timestamp"], errors="coerce")
    sp = plays[plays["session_id"].astype(str) == str(e["session_id"])].sort_values("_ts")
    t0 = pd.Timestamp(e["start_ts"])
    t1 = pd.Timestamp(e["end_ts"])
    lo = max(sp["_ts"].min(), t0 - pd.Timedelta(minutes=22))
    hi = min(sp["_ts"].max(), t1 + pd.Timedelta(minutes=22))
    win = sp[(sp["_ts"] >= lo) & (sp["_ts"] <= hi)]
    x = (win["_ts"] - t0).dt.total_seconds() / 60
    in_ep = (win["_ts"] >= t0) & (win["_ts"] <= t1)

    fig, ax = _new_fig(
        "What a binge actually looks like",
        "One real session: each dot is a video, coloured by topic; the highlighted band is the binge",
        f"Donor {e['collection_id'][:8]}…, {str(e['start_ts'])[:10]}. Binge: "
        f"{e['n_distinct']} distinct {e['dominant_niche']} videos in "
        f"{e['duration_min']:.0f} minutes; ordinary varied scrolling before and after.")
    ep_len = (t1 - t0).total_seconds() / 60
    ax.axvspan(0, ep_len, color=ACCENT2, alpha=0.22, zorder=0)
    rng = np.random.default_rng(5)
    y = pd.Series(rng.uniform(0.25, 0.75, len(win)), index=win.index)

    # Colour the surrounding videos by their topic, one colour per niche, so
    # the binge's sameness contrasts with the session's ordinary variety.
    labels = data_access.load_video_labels(set(win["item_id"]))
    niche = win["item_id"].map(lambda i: (labels.get(i) or {}).get("niche_name"))
    palette = ["#64b5f6", "#81c784", "#e57373", "#b39ddb", "#4db6ac", "#f06292",
               "#a1887f", "#90caf9", "#aed581", "#ffd54f", "#9575cd", "#4dd0e1"]
    others = win[~in_ep]
    unmapped = others[niche[others.index].isna()]
    ax.scatter(x[unmapped.index], y[unmapped.index], s=60, color="#3a4150",
               alpha=0.55, label="unmapped videos")
    for k, (nname, grp) in enumerate(others[niche[others.index].notna()]
                                     .groupby(niche, sort=False)):
        ax.scatter(x[grp.index], y[grp.index], s=95,
                   color=palette[k % len(palette)], alpha=0.9)
    ax.scatter(x[in_ep], y[in_ep], s=150, color=ACCENT2, edgecolors=BG,
               linewidths=1.2, label=f"the binge: {e['dominant_niche']}")
    ax.annotate(f"{e['n_distinct']} similar videos · {e['duration_min']:.0f} min",
                xy=(ep_len / 2, 0.88), fontsize=16, color=ACCENT2, ha="center",
                fontweight="bold")
    ax.annotate("every other colour = a different topic", xy=(0.02, 0.96),
                xycoords="axes fraction", fontsize=13, color=MUTED)
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_xlabel("minutes (0 = binge starts)")
    ax.legend(loc="lower left", frameon=False, fontsize=13)
    _save(fig, "fig2_anatomy.png")




def fig3_what_gets_binged(base: dict) -> None:
    """Niche enrichment ratios with the politics contrast."""
    rows = sorted(base["niche_enrichment"], key=lambda r: r["ratio"] or 0)
    names = [r["niche"] for r in rows]
    ratios = [r["ratio"] for r in rows]
    colours = [BAD if not r["fdr_significant"] else ACCENT for r in rows]
    fig, ax = _new_fig(
        "Reviews, recipes & how-tos get binged — politics doesn't",
        "How much more often a topic appears in binges than in the same people's overall viewing",
        "Bars = observed ÷ expected from 500 matched random draws of each donor's own diet. "
        "All blue bars significant (FDR); politics is the one tested topic that is not.")
    fig.subplots_adjust(left=0.26, right=0.94)
    bars = ax.barh(range(len(rows)), ratios, color=colours, height=0.72)
    ax.axvline(1.0, color=MUTED, lw=1, ls="--")
    ax.annotate("1× = no more than expected", xy=(1.05, len(rows) - 0.6),
                fontsize=12, color=MUTED)
    for i, (r, b) in enumerate(zip(rows, bars)):
        label = f"{r['ratio']:.1f}×" + ("  (not significant)" if not r["fdr_significant"] else "")
        ax.text(b.get_width() + 0.08, i, label, va="center", fontsize=12.5,
                color=BAD if not r["fdr_significant"] else FG)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(names, fontsize=12.5)
    ax.set_xlim(0, max(ratios) * 1.22)
    ax.set_xlabel("over-representation in binges")
    _save(fig, "fig3_what_gets_binged.png")




def fig4_creator_loyalty(base: dict) -> None:
    """Observed vs chance share of single-creator-dominated binges."""
    a = base["author_concentration"]
    fig, ax = _new_fig(
        "Half of all binges are mostly one creator",
        "Share of binges where a single creator accounts for at least half the videos",
        "Chance = matched random draws from each donor's own viewing. Suggests creator "
        "loyalty (following someone you like), not algorithmic topic spirals.")
    vals = [a["null_pct_author_dominated"] * 100, a["obs_pct_author_dominated"] * 100]
    bars = ax.bar([0, 1], vals, color=[MUTED, ACCENT2], width=0.5)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["expected by chance", "observed in real binges"], fontsize=15)
    for b, v, c in zip(bars, vals, [FG, ACCENT2]):
        ax.text(b.get_x() + b.get_width() / 2, v + 1.2, f"{v:.1f}%", ha="center",
                fontsize=24, fontweight="bold", color=c)
    ax.set_ylim(0, 60)
    ax.set_ylabel("% of binges dominated by one creator")
    _save(fig, "fig4_creator_loyalty.png")




def fig5_not_random(null_rows: list) -> None:
    """Observed episode counts vs the shuffled chance baseline."""
    df = pd.DataFrame(null_rows)
    fired = df[df["obs_episodes"] > 0]
    rng = np.random.default_rng(3)
    fig, ax = _new_fig(
        "Binges are real patterns, not coincidence",
        "Episodes found in each person's real history vs the same videos in shuffled order",
        "Each person's viewing re-ordered at random 200 times and re-scanned. "
        "57 of 73 real histories beat their own chance baseline (FDR-corrected).")
    xo = rng.normal(1, 0.05, len(fired))
    xn = rng.normal(0, 0.05, len(fired))
    null_means = pd.to_numeric(fired["null_mean"], errors="coerce").fillna(0)
    ax.scatter(xn, null_means + 0.05, s=70, color=MUTED, alpha=0.8)
    ax.scatter(xo, fired["obs_episodes"] + 0.05, s=70, color=ACCENT, alpha=0.85)
    ax.set_yscale("log")
    ax.set_ylim(0.04, 120)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["same videos,\nshuffled order", "real viewing\norder"], fontsize=15)
    ax.set_ylabel("binge episodes per person (log scale)")
    ax.annotate("essentially zero", xy=(0, 0.09), fontsize=14, color=MUTED, ha="center")
    ax.annotate("1 to 75 episodes", xy=(1, 90), fontsize=14, color=ACCENT, ha="center")
    _save(fig, "fig5_not_random.png")




def fig6_no_rabbit_hole(ep: pd.DataFrame) -> None:
    """Episode geometry: the directed-descent region is empty."""
    fig, ax = _new_fig(
        "No evidence of the 'rabbit-hole descent'",
        "Every binge, plotted by how far it travelled and how directed its path was",
        "A 'descent' would travel far in a consistent direction (upper-right region). "
        "Across all 389 binges — and all 10 alternative definitions tested — that region is empty.")
    d = pd.to_numeric(ep["diameter"], errors="coerce")
    s = pd.to_numeric(ep["straightness"], errors="coerce").fillna(0)
    ax.scatter(d, s, s=60, color=ACCENT, alpha=0.6, edgecolors="none")
    ax.add_patch(Rectangle((0.5, 0.5), 0.6, 0.5, facecolor=BAD, alpha=0.12,
                           edgecolor=BAD, lw=1.5, ls="--"))
    ax.annotate('the "led ever deeper" zone:\n0 binges land here', xy=(0.80, 0.72),
                fontsize=16, color=BAD, ha="center", fontweight="bold")
    ax.annotate("binges sit in one topic\nand stay there", xy=(0.30, 0.07),
                fontsize=14, color=ACCENT, ha="center")
    ax.set_xlabel("how far the binge travelled in topic space (diameter)")
    ax.set_ylabel("how directed the path was (straightness)")
    ax.set_xlim(0, 1.1)
    ax.set_ylim(0, 1.0)
    _save(fig, "fig6_no_rabbit_hole.png")




def fig7_habit_and_exit(p2: dict) -> None:
    """Phase 2 combo: recurrence + the session-finale effect."""
    fig = plt.figure(figsize=FIGSIZE, dpi=DPI)
    fig.text(0.05, 0.93, "Binges are habits — and finales, not traps",
             fontsize=27, fontweight="bold", color=FG)
    fig.text(0.05, 0.865, "People return to their binge topic for weeks; "
             "after a binge, the session winds down sooner", fontsize=16, color=MUTED)
    fig.text(0.05, 0.035, "Left: share of a person's binge pairs on the same topic vs chance "
             f"(median return after {p2['rq9']['median_return_gap_days']:.0f} days). "
             "Right: session time remaining after a binge vs the same point in binge-free "
             "sessions (paired within person).", fontsize=10.5, color=MUTED, style="italic")

    ax1 = fig.add_axes([0.08, 0.16, 0.36, 0.60])
    r9 = p2["rq9"]
    vals = [r9["null_same_niche_pair_share"] * 100, r9["obs_same_niche_pair_share"] * 100]
    bars = ax1.bar([0, 1], vals, color=[MUTED, VIOLET], width=0.5)
    for b, v in zip(bars, vals):
        ax1.text(b.get_x() + b.get_width() / 2, v + 0.4, f"{v:.1f}%", ha="center",
                 fontsize=20, fontweight="bold", color=FG)
    ax1.set_xticks([0, 1])
    ax1.set_xticklabels(["chance", "observed"], fontsize=14)
    ax1.set_ylabel("% of binge pairs on the same topic")
    ax1.set_title("the same person re-binges\nthe same topic (6× chance)", fontsize=15, color=VIOLET, pad=12)
    for s in ("top", "right"):
        ax1.spines[s].set_visible(False)

    ax2 = fig.add_axes([0.58, 0.16, 0.36, 0.60])
    r10 = p2["rq10"]
    vals2 = [r10["median_remaining_control_min"], r10["median_remaining_after_episode_min"]]
    bars2 = ax2.bar([0, 1], vals2, color=[MUTED, GOOD], width=0.5)
    for b, v in zip(bars2, vals2):
        ax2.text(b.get_x() + b.get_width() / 2, v + 0.4, f"{v:.0f} min", ha="center",
                 fontsize=20, fontweight="bold", color=FG)
    ax2.set_xticks([0, 1])
    ax2.set_xticklabels(["no binge\n(same point in session)", "right after\na binge"], fontsize=13)
    ax2.set_ylabel("session time remaining (min)")
    ax2.set_title("sessions end sooner after a binge\n(absorbing, then done)", fontsize=15, color=GOOD, pad=12)
    for s in ("top", "right"):
        ax2.spines[s].set_visible(False)
    _save(fig, "fig7_habit_and_exit.png")




def fig8_prediction(p3: list) -> None:
    """Phase 3: how much warning does a binge give?"""
    by = {r["feature_set"]: r for r in p3 if "pr_auc" in r}
    if not by:
        print("  (phase 3 results unavailable — skipping fig8)")
        return
    sets = ["momentum_only", "precursor_only", "full"]
    labels = ["the stream is already\nnarrowing (momentum)",
              "behaviour: dwell, time,\nlikes & searches", "everything\ncombined"]
    lifts = [by[s].get("lift_at_top1pct") or 0 for s in sets]
    fig, ax = _new_fig(
        "A binge announces itself — as a choice being made",
        "Flagging 'a binge starts now' beats guessing 17×…",
        f"Logistic models, per-person time-ordered split; onsets are rare "
        f"({by['full']['base_rate']:.2%} of plays), so this is evidence about mechanism, "
        f"not a practical alarm. Lift 1× = guessing.")
    fig.text(0.05, 0.825, "…and the strongest signals are the viewer's own likes, "
             "follows and searches", fontsize=16, ha="left", color=MUTED)
    bars = ax.bar(range(3), lifts, color=[ACCENT, ACCENT2, GOOD], width=0.5)
    ax.axhline(1, color=MUTED, ls="--", lw=1.2)
    ax.annotate("1× = guessing", xy=(2.35, 1.15), fontsize=13, color=MUTED, ha="right")
    for b, v in zip(bars, lifts):
        ax.text(b.get_x() + b.get_width() / 2, v + max(lifts) * 0.02, f"{v:.1f}×",
                ha="center", fontsize=21, fontweight="bold", color=FG)
    ax.set_xticks(range(3))
    ax.set_xticklabels(labels, fontsize=13.5)
    ax.set_ylabel("lift over chance")
    _save(fig, "fig8_prediction.png")




def main() -> None:
    os.makedirs(FIG_DIR, exist_ok=True)
    sm = pd.read_parquet(os.path.join(OUT_DIR, "episode_donor_summary_v1.parquet"))
    ep = pd.read_parquet(os.path.join(OUT_DIR, "episodes_v1.parquet"))
    base = json.load(open(os.path.join(OUT_DIR, "base_rates_v1.json")))
    null_rows = json.load(open(os.path.join(OUT_DIR, "episode_null_v1.json")))
    p2 = json.load(open(os.path.join(OUT_DIR, "phase2_v1.json")))
    try:
        p3 = json.load(open(os.path.join(OUT_DIR, "phase3_v1.json")))
    except FileNotFoundError:
        p3 = []

    fig0_headline()
    fig1_rare_but_universal(sm)
    fig2_anatomy(ep)
    fig3_what_gets_binged(base)
    fig4_creator_loyalty(base)
    fig5_not_random(null_rows)
    fig6_no_rabbit_hole(ep)
    fig7_habit_and_exit(p2)
    fig8_prediction(p3)
    print("Done.")




if __name__ == "__main__":
    main()
